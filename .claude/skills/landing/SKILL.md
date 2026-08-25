---
name: landing
description: Start a quota-budgeted coding task with a hard remaining-quota floor and graceful handoff.
argument-hint: '(leave|spend) [weekly|5h] <percent> -- <task>'
disable-model-invocation: true
allowed-tools: Bash(landing *)
---

Use Agent Landing for this task.

## Do not parse the arguments yourself

Pass the raw invocation straight through:

```bash
landing invoke "$ARGUMENTS" --provider claude
```

`landing invoke` is the single parser shared by Claude Code and Codex, so the two
hosts cannot disagree about what an invocation means.

**If the command exits non-zero**, it printed a refusal explaining what was wrong,
the expected syntax, and possibly a suggested correction. Show that message to the
user and **stop**. Do not start implementation, do not retry with a guessed value,
and do not start a budget by calling `landing start` directly. Starting work
without a budget is the one outcome worse than refusing: the user would believe
they were protected while nothing was guarding their quota.

**If it succeeds**, it printed an `interpretation` line. Show that line to the user
before beginning work, so a misunderstanding surfaces before any quota is spent.

## Syntax

```text
/landing (leave|spend) [weekly|5h] <percent> -- <task>
```

- `leave 70` — stop while at least **70% remains**.
- `spend 15` — spend at most **15 percentage points**, whatever that leaves.
- One of the two keywords is **required**. A bare number is refused, because a
  dropped keyword would otherwise stay a legal invocation carrying the opposite
  meaning: `spend 15` degrading to `15` would authorize 76 points of spend
  instead of 15, with nothing to catch it.
- `--` is optional; the first word that is neither a keyword, a window, nor a
  number starts the task.
- The settings may come **before or after** the task, whichever reads more
  naturally: `leave 70 -- fix the tests` and `fix the tests leave 70` are the
  same request. A trailing group is only recognized when it is complete (a
  keyword plus a number), so a number or the word `leave` occurring inside the
  task text is never mistaken for settings.
- `weekly` or `5h` is **optional**. Omit it and Agent Landing uses the longest
  window your plan exposes — weekly where it exists, the five-hour window on
  plans that only have that one. Landing feasibility is checked on *every*
  window either way, so the one you did not name can never be silently
  exhausted. Name it only to put the floor itself on that window.

Examples:

- `/landing leave 70 -- refactor auth and add tests`
- `/landing spend 15 -- fix the failing checkout tests`
- `/landing leave 30 fix the checkout tests`   (the `--` is optional)
- `/landing leave 5h 30 -- investigate the flaky test`   (pin the floor to one window)

## While working

Execute the requested task normally. Treat the floor as a hard lower bound, never
as a spending target. If the task completes early, immediately run
`landing complete` and stop — never invent extra work, tests, refactors, or polish
to consume the unused allowance.

The budget contract outlives a single task: if the user sends a new instruction
afterwards, the guard automatically starts a new segment under the same floor.

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — run `landing cancel`. That ends the contract and stands
the guard down; a later instruction will not restart the floor. Use `landing
complete` only when the *task* is done and the budget should stay in force.

When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly.
