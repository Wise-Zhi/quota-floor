"""The handoff: what the user is left holding when a budget runs out.

It is saved under Landing's own state directory, never in the user's folder. A
budget is not always attached to a project, and when it is, `AGENT_HANDOFF-N.md`
files piling up in it are clutter nobody asked for. The model writes no file:
in LAND its final reply *is* the handoff, and the Stop hook saves it. That also
removed the one write the LAND gate used to have to let through.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from .models import PolicyDecision, TaskSession

NO_TASK_RECORDED = (
    "Not recorded at invocation: the budget was attached to work already under way. "
    "See the conversation and the working folder state below."
)

#: Anything that is not a word character becomes a dash, so a task name can
#: never carry a path separator out of the handoff directory.
_UNSAFE = re.compile(r"[^\w]+", re.UNICODE)


def _slug(task: str) -> str:
    slug = _UNSAFE.sub("-", task).strip("-_").lower()
    return slug[:40].rstrip("-_") or "handoff"


def new_handoff_path(directory: Path, session: TaskSession) -> Path:
    """A fresh, never-existing file for this handoff, named by time and task."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{_slug(session.task)}" if session.task else f"{stamp}-handoff"
    path = directory / f"{base}.md"
    index = 2
    while path.exists():
        path = directory / f"{base}-{index}.md"
        index += 1
    return path


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def landing_prompt(session: TaskSession, decision: PolicyDecision) -> str:
    return f"""AGENT LANDING MODE
The user required this task to preserve at least {session.floor_percent:.1f}% of the {session.window} quota.
Current remaining quota is {decision.remaining_percent:.1f}%. The estimated landing reserve is {decision.reserve_percent:.1f}%.

Do not start new implementation work or new workstreams, and do not edit any file. Do not write a handoff file either: your final reply to the user IS the handoff, and Landing saves it automatically when your turn ends.
Write that final reply now. It must contain:
- Original task / goal
- What is completed and verified
- Files changed and why
- Tests/validation already run and their results
- What is incomplete or unverified
- Failed approaches or important decisions, if any
- Exact next steps in priority order
- A concise resume prompt for the next agent/session

Use only the minimal read-only inspection needed to make it accurate. Do not perform unrelated fixes, refactors, broad searches, or long test suites. After that reply, stop."""


def _header(session: TaskSession) -> str:
    saved = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"- Task: {session.task or NO_TASK_RECORDED}\n"
        f"- Working folder: `{session.cwd}`\n"
        f"- Provider / window: `{session.provider}` / `{session.window}`\n"
        f"- Saved: {saved}\n"
    )


def save_reply_handoff(session: TaskSession, reply: str, directory: Path) -> Path:
    """Save the model's final LAND reply as this task's handoff."""
    directory.mkdir(parents=True, exist_ok=True)
    path = new_handoff_path(directory, session)
    path.write_text(f"# Agent Handoff\n\n{_header(session)}\n---\n\n{reply.strip()}\n", encoding="utf-8")
    return path


def write_fallback_handoff(session: TaskSession, decision: PolicyDecision, directory: Path) -> Path:
    """Write a handoff from local state alone, with no model turn to spend."""
    cwd = Path(session.cwd).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = new_handoff_path(directory, session)

    if _git(cwd, "rev-parse", "--is-inside-work-tree") == "true":
        folder_state = f"""Branch: `{_git(cwd, "branch", "--show-current") or "unknown"}`

### Git status
```text
{_git(cwd, "status", "--short") or "clean working tree"}
```

### Unstaged diff summary
```text
{_git(cwd, "diff", "--stat") or "no unstaged diff"}
```

### Staged diff summary
```text
{_git(cwd, "diff", "--cached", "--stat") or "no staged diff"}
```"""
    else:
        folder_state = (
            "This folder is not a git repository, so there is no diff to summarize. "
            "Inspect recently modified files directly."
        )

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

## Working Folder
`{cwd}`

{folder_state}

## Completed / Verified
Unknown to the deterministic fallback. Inspect the folder and transcript before claiming completion.

## Incomplete / Unverified
The task may be partially complete. Do not assume changed files are correct or tested.

## Next Steps
1. Read this file and inspect the working folder (`git diff` / `git status` if it is a repository).
2. Recover the previous session transcript if available.
3. Determine which acceptance criteria are already satisfied.
4. Continue only after establishing a new quota budget.

## Resume Prompt
Continue the task described above from the current state of the working folder. First inspect what changed and this handoff; do not redo completed work. Establish a fresh quota budget before implementation.
"""
    path.write_text(content, encoding="utf-8")
    return path
