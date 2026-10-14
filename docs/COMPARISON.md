# How Landing compares

Several tools already help with agent quota. This page explains where Landing
fits among them, based on each tool's own description as of October 2026. If
something here is wrong or out of date, please open an issue.

| | Unit | When it acts | Unknown quota | What you get at the stop |
| --- | --- | --- | --- | --- |
| Claude Code `--max-budget-usd` | API dollars, one `-p` run | when the dollar cap is reached | n/a | an error result |
| claude-usage-guard | subscription windows | when a window is exhausted | fails open (work continues) | a scheduled wake-up after the reset |
| Quota viewers (codex-quota, quse, cq, ai-quota) | subscription windows | never: they only display | shown as unavailable | nothing; they enforce no limit |
| **Agent Landing** | **% of each subscription window** | **at your floor, minus a measured landing reserve** | **never read as full: budget stays in CONSERVE** | **a handoff saved outside your project** |

## Claude Code `--max-budget-usd`

A built-in cap on the dollar cost of a single non-interactive run
(`claude -p ... --max-budget-usd 2.50`). It suits scripted and CI use.

It does not fit an interactive subscription session. It applies only to print
mode, and it is measured in dollars, while a Claude.ai plan is limited by percent
of a five-hour and a weekly window. You cannot use it to say "leave me 70% of my
week". It also stops by ending the run. Nothing is reserved for writing down
what was done.

## claude-usage-guard

A Claude Code plugin whose hooks warn as subscription usage approaches its
limits, and block new work once a window is **exhausted**. Its motivating case is
extra usage, where work would otherwise continue at API rates.

The differences:

- **Threshold.** It blocks at exhaustion. Landing blocks at a floor you choose
  (`leave 70`, `spend 15`), so the rest of the week stays yours.
- **Quota source.** By its own description it reads an undocumented endpoint. Landing reads
  the `rate_limits` field Claude Code passes to the status line, and Codex's
  app-server `account/rateLimits/read`.
- **Failure mode.** By its own description it fails open: no usage data means
  work continues. Landing never reads unknown or stale quota as headroom. An
  active budget with no reading stays in CONSERVE and never returns to NORMAL.
  If the gate itself fails while a budget is active, the write is denied.
- **Landing.** At exhaustion there is no quota left for a summary. Landing stops
  at a reserve above your floor, sized from observed burn, and spends that
  reserve on a handoff.

The two can coexist. Exhaustion blocking also covers sessions where you never
set a budget.

## Quota viewers

Tools such as codex-quota, quse, cq and ai-quota display the five-hour and
weekly windows for Codex (and, for some, Claude and Gemini). They are useful,
and Landing's `landing status` and dashboard cover similar ground. They are
read-only by design: they don't stop the agent.

## What only Landing does

1. **A user-chosen percentage floor, per task.** `/landing leave 70 fix the
   tests` or `/landing spend 15 fix the tests`, parsed by a hook in code, not by
   the model. The floor is a limit, never a target: a task that finishes early
   stops early.
2. **Unknown is not 100%.** Missing, stale, or unreadable quota keeps the
   budget in CONSERVE, never NORMAL. A gate error under an active budget
   denies the write. A guard that fails open stops guarding exactly when it
   matters.
3. **A landing reserve and a handoff.** Writes stop *before* the floor, at a
   margin measured from your own burn rate. The agent's final reply becomes a
   resumable handoff saved to `~/.landing/handoffs/`. If no model turn is
   left, a deterministic one is written locally.
4. **Both hosts, one policy.** Claude Code and Codex share the same state
   machine. They differ only in how quota is read.

What Landing does not do yet is in the README's
[Known limitations](../README.md#known-limitations) and in
[VERIFICATION.md](VERIFICATION.md).
