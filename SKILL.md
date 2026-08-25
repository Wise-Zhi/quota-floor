---
name: landing
description: Run coding tasks under an explicit account-quota budget and land safely before the budget floor.
---

# Agent Landing

Use this skill when the user expresses a quota constraint such as:

- "Leave me at least 70% weekly quota."
- "Spend at most 15% on this task."
- "Stop before the five-hour quota drops below 40%."

## Start the budget before implementation

Do not parse the invocation yourself. Pass the raw arguments through:

```bash
landing invoke "<raw arguments>" --provider claude   # or --provider codex
```

The parser is shared by both hosts, so `/landing` and `$landing` cannot disagree
about what an invocation means. Syntax:

```text
(leave|spend) [weekly|5h] <percent> -- <task>
```

- `leave 70` stops while at least 70% **remains**; `spend 15` spends at most 15
  percentage points. One of the two is required -- a bare number is refused
  rather than defaulted, so a dropped keyword can never silently invert the
  meaning.
- Omitting the window applies the floor to every window the account exposes,
  stopping at whichever reaches it first. Naming one scopes the floor to it.
  Landing feasibility is checked on every window regardless.

If the command exits non-zero it refused the invocation. Show its message and
stop: starting work without a budget is worse than refusing, because the user
would believe they were protected. If it succeeds, show the `interpretation`
line before doing anything else.

The equivalent explicit commands, if you need them directly:

```bash
landing start --provider claude --window auto --leave 70 --task "<task>"
landing start --provider codex --window auto --spend 15 --task "<task>"
```

## The floor is never a target

If the task finishes early, immediately run `landing complete` and stop. Never
create extra work, tests, refactors, or polish merely to consume the unused
allowance.

The budget contract outlives one task. When the user sends a new instruction
afterwards, a new segment starts under the same floor — automatically wherever
the host exposes a `UserPromptSubmit` hook (Claude Code, and Codex from
0.149), or manually with `landing resume` on hosts that have no equivalent.

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — run `landing cancel`. That ends the contract and stands
the guard down; a later instruction will not restart the floor. Use `landing
complete` only when the *task* is done and the budget should stay in force.

## Runway telemetry

Do not estimate total task cost from semantic progress. Agent Landing may display
a local **runway** derived only from observed quota movement over time. This is
user-facing telemetry, not permission to cross the quota floor. If there are not
enough real samples, report `Learning` instead of guessing.

## Runtime behavior

The external quota guard, not the model, calculates remaining quota and landing
reserve. Follow hook-injected states:

- NORMAL: work normally.
- CONSERVE: finish the bounded objective; do not open large new workstreams.
- LAND: stop implementation, do minimal state inspection, write `AGENT_HANDOFF.md`, then stop.
- EMERGENCY: the guard writes a deterministic handoff without another model call
  and blocks further tool use.

CONSERVE is also used when quota cannot be read or is stale. Unknown quota is
never treated as available headroom.

## Handoff requirements

The handoff must state the original task, completed and verified work, changed
files, validation results, incomplete/unverified work, important decisions/failed
approaches, exact next steps, and a resume prompt.
