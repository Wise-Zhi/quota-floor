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

Translate the user's request into one of these commands:

```bash
landing start --provider claude --window seven_day --preserve 70 --task "<task>"
landing start --provider codex --window seven_day --max-spend 15 --task "<task>"
```

Do not silently weaken the user's quota floor. A floor is never a target: if the task finishes early, immediately run `landing complete` and stop. Never create extra work, extra tests, refactors, or polish merely to consume the unused allowance.

## Runway telemetry

Do not estimate total task cost from semantic progress. Agent Landing may display a local **runway** derived only from observed quota movement over time. This is user-facing telemetry, not permission to cross the quota floor. If there are not enough real samples, report `Learning` instead of guessing.

## Runtime behavior

The external quota guard, not the model, calculates remaining quota and landing reserve. Follow hook-injected states:

- NORMAL: work normally.
- CONSERVE: finish the bounded objective; do not open large new workstreams.
- LAND: stop implementation, do minimal state inspection, write `AGENT_HANDOFF.md`, then stop.
- EMERGENCY: the guard writes a deterministic handoff without another model call and blocks further tool use.

## Handoff requirements

The handoff must state the original task, completed and verified work, changed files, validation results, incomplete/unverified work, important decisions/failed approaches, exact next steps, and a resume prompt.
