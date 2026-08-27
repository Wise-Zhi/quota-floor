---
name: landing
description: This skill should be used when the user asks to "leave me 70% quota", "spend at most 15% on this", "stop before my weekly quota drops below 40%", or otherwise asks for a coding task to be run under an account-quota budget with a hard floor on what must remain afterwards.
version: 0.1.0
---

Use Agent Landing for this task.

## Start with the task, not the guard

Any user-visible progress message must describe only the real work requested.
Never announce that you are reading this skill, checking Landing rules, parsing
the budget, validating quota, or setting up the guard. If the host requires an
opening update, use a task-only sentence such as `I’ll playtest the current build
and report player-facing problems without changing code.` Then act immediately.

When the budget must be mentioned, `spend N` means **spend at most N percentage
points**. Never call it `N quota`, `N of quota`, an allowance to consume, or a
target. Omit the number entirely when it does not help the user understand the
task result.

## Codex composer selection

The syntax row in the Codex composer is an autocomplete candidate, not a
persistent argument hint. Select **Agent Landing** before entering arguments;
pressing Space while the candidate is merely visible dismisses it on Codex
`0.149.0-alpha.4.3`. The configured editable starter is
`Use $landing leave 75 <task>`. This composer behavior does not change the
parser rules below.

## Do not parse the arguments yourself

Build the raw invocation from the **entire user message**, preserving task text
on both sides of the Agent Landing skill chip. Remove only the `$landing` skill
marker itself, then join the remaining text in its original order and pass that
through. For example, `playtest this [Agent Landing] spend 20` becomes
`playtest this spend 20`; the task before the chip must not disappear.

```bash
landing invoke "<whole message minus the skill marker>" --provider codex
```

`landing invoke` is the single parser shared by Codex and Claude Code, so the two
hosts cannot disagree about what an invocation means.

**If the command exits non-zero**, it printed a refusal explaining what was wrong,
the expected syntax, and possibly a suggested correction. Show that message to the
user and **stop**. Do not start implementation, do not retry with a guessed value,
and do not start a budget by calling `landing start` directly. Starting work
without a budget is the one outcome worse than refusing: the user would believe
they were protected while nothing was guarding their quota.

**If it succeeds**, begin the requested task immediately. Do not narrate the
invocation, validation, quota snapshot, reserve calculation, session state, or
cancel instructions. If confirmation is useful, compress the interpretation to
one short sentence such as `Landing active: spend at most 10 points; stopping
earlier if the task finishes.` Never describe the allowance as a target.

Landing is a constraint on the task, not the task itself. The text after the
settings is the work to perform. Complete that work normally and report its
observable result. Use ordinary contextual judgment for natural requests such as
`try it`, `test it`, or `take a look`; infer their subject from the current
project and conversation exactly as you would without Landing. Do not downgrade
them to an empty task. Only the deterministic parser decides that a task is
missing.

This skill also triggers on a plain-language budget request, not only on an
explicit invocation. Pass the user's own words through unchanged and **never
supply a percentage they did not give**. A request with no number is refused by
the parser -- show that refusal and ask which floor they want, rather than
choosing one for them. Starting a budget the user did not specify is as wrong as
starting none at all: either way the number guarding their quota is not theirs.

## Syntax

```text
$landing (leave|spend) [weekly|5h] <percent> <task>
```

- `leave 70` — stop while at least **70% remains**.
- `spend 15` — spend at most **15 percentage points**, whatever that leaves.
- One of the two keywords is **required**. A bare number is refused, because a
  dropped keyword would otherwise stay a legal invocation carrying the opposite
  meaning: `spend 15` degrading to `15` would authorize 76 points of spend
  instead of 15, with nothing to catch it.
- The first word that is neither a keyword, a window, nor a number starts the task.
- The settings may come **before or after** the task, whichever reads more
  naturally: `leave 70 fix the tests` and `fix the tests leave 70` are the
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

- `$landing leave 70 refactor auth and add tests`
- `$landing spend 15 fix the failing checkout tests`
- `$landing leave 30 fix the checkout tests`
- `$landing leave 5h 30 investigate the flaky test`   (pin the floor to one window)

## While working

Execute the requested task normally. Treat the floor as a hard lower bound, never
as a spending target. If the task completes early, immediately run
`landing complete` and stop — never invent extra work, tests, refactors, or polish
to consume the unused allowance.

The budget belongs only to this requested task. Completion freezes this turn;
the next real user instruction releases the finished contract instead of
renewing it. Later tasks and other chats do not inherit the floor. A later
Landing invocation starts a fresh budget.

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — **do not run a cancel command**. An agent cannot lift its
own quota floor, and at the boundary the attempt is denied. Tell the user to send
`$landing cancel` as their whole message; the guard stands down before the next
turn begins. Use `landing complete` only when the *task* is done and the budget
should end before the task finishes.

When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly.

## User-visible output

Lead with what the requested task accomplished, not with Landing internals. Keep
ordinary budget confirmation to one sentence and omit it from later progress
updates. Do not dump current quotas, reserve percentages, state names, segment
numbers, or cancellation help unless the user asks or must act on a problem.
