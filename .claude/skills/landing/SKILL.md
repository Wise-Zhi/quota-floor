---
name: landing
description: 'Run a coding task under an account-quota budget with a hard floor on what must remain. Use when the user asks to "leave me 70% quota", "spend at most 15% on this", "留 70", or otherwise caps how much quota a task may use. Example: `/landing leave 70 fix the tests` stops while at least 70% remains; `/landing spend 20 fix the tests` spends at most 20 percentage points.'
argument-hint: 'leave|spend <percent> [task]'
allowed-tools: Bash(landing *)
version: 0.1.0a2
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
landing invoke "<the user's whole message>" --provider claude
```

Pass the user's own words through unchanged -- the parser reads plain phrasing
such as "leave me 70%" or "spend at most 15%" itself -- and **never supply a
percentage or a leave/spend direction they did not give**. If it exits non-zero
it printed a refusal: show that message and stop. Starting work without a budget
is the one outcome worse than refusing -- the user would believe they were
protected while nothing was guarding their quota.

## Meaning

- `leave 70` — stop while at least **70% remains**.
- `spend 15` — spend at most **15 percentage points**. It is a ceiling, never an
  allowance to consume or a target.
- One of the two is always required; a bare number means nothing, so that a
  dropped keyword can never silently become the opposite budget.

## While working

Execute the requested task normally. Treat the floor as a hard lower bound, never
as a spending target. When the task is done, stop — never invent extra work,
tests, refactors, or polish to consume the unused allowance. There is nothing to
run: the budget ends by itself when this turn ends.

Order the work by value: do the most important part of the task that is useful
on its own first, and leave each part finished before starting the next. LAND can
arrive at any point, and what is already finished is what the handoff carries, so
it should be the part the user most wanted. This changes the order of the work,
never its amount: do not estimate how much fits in the budget, do not cut scope
up front, and still stop as soon as the whole task is done.

Landing is a constraint on the task, not the task itself. Use ordinary contextual
judgment for natural requests such as `try it` or `take a look`; infer their
subject from the project and conversation exactly as you would without Landing.

The budget covers this instruction only and ends with this turn; a later Landing
invocation starts a fresh one.

When hooks inject CONSERVE, LAND, EMERGENCY, or STOPPED state, follow them exactly.

## Ending the budget

If the user wants the budget itself to end — "stop watching my quota", "forget the
budget", "cancel that" — **do not run a cancel command**. An agent cannot lift its
own quota floor, and at the boundary the attempt is denied. Tell the user to send
`/landing cancel` as their whole message; the hook stands the guard down before the
next turn begins.
