from __future__ import annotations

import subprocess
from pathlib import Path

from .models import PolicyDecision, TaskSession


def reserve_handoff_path(session: TaskSession) -> Path:
    """Claim one non-destructive handoff path for this task.

    Once claimed, the same task keeps updating the same file. An existing file
    seen before the claim belongs to earlier work and is never overwritten.
    """
    cwd = Path(session.cwd).resolve()
    requested = Path(session.handoff_path)
    path = requested if requested.is_absolute() else cwd / requested
    if session.handoff_path_reserved:
        return path

    if path.exists():
        index = 2
        while True:
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                path = candidate
                break
            index += 1

    session.handoff_path = str(path) if requested.is_absolute() else str(path.relative_to(cwd))
    session.handoff_path_reserved = True
    return path


NO_TASK_RECORDED = (
    "Not recorded at invocation: the budget was attached to work already under way. "
    "See the conversation and the repository state below."
)


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, timeout=5, check=False
        )
        return (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def landing_prompt(session: TaskSession, decision: PolicyDecision) -> str:
    return f"""AGENT LANDING MODE
The user required this task to preserve at least {session.floor_percent:.1f}% of the {session.window} quota.
Current remaining quota is {decision.remaining_percent:.1f}%. The estimated landing reserve is {decision.reserve_percent:.1f}%.

Do not start new implementation work or new workstreams. Preserve the current repository state and create or update this task's reserved handoff file, `{session.handoff_path}`, now. Do not write a different handoff file.
The handoff must contain:
- Original task / goal
- What is completed and verified
- Files changed and why
- Tests/validation already run and their results
- What is incomplete or unverified
- Failed approaches or important decisions, if any
- Exact next steps in priority order
- A concise resume prompt for the next agent/session

Use only minimal read-only inspection needed to make the handoff accurate. Do not perform unrelated fixes, refactors, broad searches, or long test suites. After the handoff is written, stop."""


def write_fallback_handoff(session: TaskSession, decision: PolicyDecision) -> Path:
    cwd = Path(session.cwd).resolve()
    path = reserve_handoff_path(session)
    status = _git(cwd, "status", "--short") or "clean working tree"
    diff_stat = _git(cwd, "diff", "--stat") or "no unstaged diff"
    staged_stat = _git(cwd, "diff", "--cached", "--stat") or "no staged diff"
    branch = _git(cwd, "branch", "--show-current") or "unknown"

    content = f"""# Agent Handoff (Emergency Fallback)

## Original Task
{session.task or NO_TASK_RECORDED}

## Why Work Stopped
The quota guard entered **{decision.state.value}** mode.

- Provider: `{session.provider}`
- Quota window: `{session.window}`
- Required final floor: **{session.floor_percent:.1f}% remaining**
- Last observed remaining: **{decision.remaining_percent:.1f}%**
- Estimated landing reserve: **{decision.reserve_percent:.1f}%**

This file was generated deterministically without another model call so state would not be lost.

## Repository State
Branch: `{branch}`

### Git status
```text
{status}
```

### Unstaged diff summary
```text
{diff_stat}
```

### Staged diff summary
```text
{staged_stat}
```

## Completed / Verified
Unknown to the deterministic fallback. Inspect the diff and transcript before claiming completion.

## Incomplete / Unverified
The task may be partially complete. Do not assume changed files are correct or tested.

## Next Steps
1. Read this file and inspect `git diff` / `git status`.
2. Recover the previous session transcript if available.
3. Determine which acceptance criteria are already satisfied.
4. Continue only after establishing a new quota budget.

## Resume Prompt
Continue the task described above from the current repository state. First inspect the existing diff and this handoff; do not redo completed work. Establish a fresh quota budget before implementation.
"""
    path.write_text(content, encoding="utf-8")
    return path
