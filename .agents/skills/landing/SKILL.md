---
name: landing
description: Start a quota-budgeted coding task with a hard remaining-quota floor and graceful handoff.
---

Use Agent Landing for this task.

## Codex composer selection

The syntax row in the Codex composer is an autocomplete candidate, not a
persistent argument hint. Select **Agent Landing** before entering arguments;
pressing Space while the candidate is merely visible dismisses it on Codex
`0.149.0-alpha.4.3`. The configured editable starter is
`Use $landing leave 75 -- <task>`. This composer behavior does not change the
parser rules below.

## Do not parse the arguments yourself

Pass the raw text that followed `$landing` straight through, unmodified:

```bash
landing invoke "<raw arguments>" --provider codex
```

`landing invoke` is the single parser shared by Codex and Claude Code, so the two
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
$landing (leave|spend) [weekly|5h] <percent> -- <task>
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
- `weekly` or `5h` is **optional** and **scopes** the floor. Omit it and the
  floor guards every window the plan exposes, stopping at whichever reaches it
  first — which is what you want, since a plan can expose both at once and the
  five-hour one is usually what binds inside a session. Name a window only to
  deliberately exclude the other. Landing feasibility is checked on *every*
  window either way, so a scoped-out window can never be silently exhausted.

Examples:

- `$landing leave 70 -- refactor auth and add tests`
- `$landing spend 15 -- fix the failing checkout tests`
- `$landing leave 30 fix the checkout tests`   (the `--` is optional)
- `$landing leave 5h 30 -- investigate the flaky test`   (pin the floor to one window)

## While working

Execute the requested task normally. Treat the floor as a hard lower bound, never
as a spending target. If the task completes early, immediately run
`landing complete` and stop — never invent extra work, tests, refactors, or polish
to consume the unused allowance.

The budget contract outlives a single task: if the user sends a new instruction
afterwards, a new segment starts under the same floor. Codex does this
automatically when the `UserPromptSubmit` hook is installed (0.149 and later);
run `landing resume` by hand on builds or setups without it.

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — **do not run a cancel command**. An agent cannot lift its
own quota floor, and at the boundary the attempt is denied. Tell the user to send
`$landing cancel` as their whole message; the guard stands down before the next
turn begins. Use `landing complete` only when the *task* is done and the budget
should stay in force.

When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly.
