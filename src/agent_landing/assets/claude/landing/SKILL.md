---
name: landing
description: This skill should be used when the user asks to "leave me 70% quota", "spend at most 15% on this", "stop before my weekly quota drops below 40%", or otherwise asks for a coding task to be run under an account-quota budget with a hard floor on what must remain afterwards. Syntax: `/landing leave 70 refactor auth` stops while at least 70% still remains; `/landing spend 20 fix the tests` spends at most 20 percentage points.
argument-hint: '<leave|spend> [weekly|5h] <percent> [task]'
allowed-tools: Bash(landing *)
version: 0.1.0a1
---

The quota budget for this instruction is normally **already open**. A
`UserPromptSubmit` hook parses the invocation and establishes it before the turn
begins, so there is nothing to set up: do not run `landing invoke` or `landing
start`, do not re-check the quota, and do not describe the guard, the parse, or
the budget to the user. Lead with the requested work and report its result.

## When you must open it yourself

The hook announces itself with `Quota guard: a budget is already active for this
instruction`. Two cases lack that note, and only in those does the budget still
need opening here:

- the user asked in plain language, with no `/landing` marker for the hook to see;
- the host does not run Landing's `UserPromptSubmit` hook at all.

```bash
landing invoke "<the whole message, minus the skill marker>" --provider claude
```

Pass the user's own words through unchanged and **never supply a percentage they
did not give**. If it exits non-zero it printed a refusal: show that message and
stop. Starting work without a budget is the one outcome worse than refusing --
the user would believe they were protected while nothing was guarding their
quota.

## Syntax

```text
/landing <leave|spend> [weekly|5h] <percent> [task]
```

- `leave 70` — stop while at least **70% remains**.
- `spend 15` — spend at most **15 percentage points**, whatever that leaves. It
  is a ceiling on spending, never an allowance to consume or a target.
- One of the two keywords is **required**. A bare number is refused, because a
  dropped keyword would otherwise stay a legal invocation carrying the opposite
  meaning: `spend 15` degrading to `15` would authorize 76 points of spend
  instead of 15, with nothing to catch it.
- The first word that is neither a keyword, a window, nor a number starts the task.
- The task is **optional**: `/landing spend 20` on its own puts a floor under the
  work already under way. The task is a label for the handoff, not a setting.
- The settings may come **before or after** the task, and task text on both sides
  of the marker is preserved: `继续/landing spend 20` is a `spend 20` budget on the
  task `继续`.
- `weekly` or `5h` is **optional** and **scopes** the floor. Omit it and the floor
  guards every window the plan exposes, stopping at whichever reaches it first.
  Landing feasibility is checked on *every* window either way, so a scoped-out
  window can never be silently exhausted.

Examples:

- `/landing leave 70 refactor auth and add tests`
- `/landing spend 15 fix the failing checkout tests`
- `/landing leave 5h 30 investigate the flaky test`   (pin the floor to one window)

## While working

Execute the requested task normally. Treat the floor as a hard lower bound, never
as a spending target. If the task completes early, immediately run
`landing complete` and stop — never invent extra work, tests, refactors, or
polish to consume the unused allowance.

Landing is a constraint on the task, not the task itself. Use ordinary contextual
judgment for natural requests such as `try it` or `take a look`; infer their
subject from the project and conversation exactly as you would without Landing.

The budget belongs only to this requested task. Completion freezes this turn; the
next real user instruction releases the finished contract instead of renewing it.
A later Landing invocation starts a fresh budget.

When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly.

## Ending the budget

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — **do not run a cancel command**. An agent cannot lift its
own quota floor, and at the boundary the attempt is denied. Tell the user to send
`/landing cancel` as their whole message; the hook stands the guard down before the
next turn begins. Use `landing complete` only when the *task* is done.
