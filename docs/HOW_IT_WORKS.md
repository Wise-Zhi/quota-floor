# How Quota Floor works

This is the detail behind the README's short version. For components and code
layout see [ARCHITECTURE.md](ARCHITECTURE.md); for what has been checked on real
hosts see [VERIFICATION.md](VERIFICATION.md).

## The landing reserve
Quota Floor treats the last part of the budget as a **landing reserve**. A requested percentage is a **floor, never a spending target**: if the task finishes with 91% remaining even though the user asked to preserve 75%, Quota Floor stops at 91%. It never invents extra work to consume the allowance.

```text
current remaining quota
        -
user's required final floor
        -
dynamic landing reserve
        =
safe development headroom
```

Example: the user says **"implement OAuth, but leave me 70% weekly quota."** If the current session's recent burn rate implies a 4% landing reserve, implementation stops around 74%, not at 70%. The remaining budget is used only to preserve state and write the handoff.

## States

- **NORMAL** — work normally.
- **CONSERVE** — quota is getting close; finish the bounded objective and avoid new workstreams.
- **LAND** — block every write, permit minimal read-only inspection, and have the agent write the handoff as its final reply; a `Stop` hook saves it to `~/.landing/handoffs/` and the task stops. Nothing is written into your folder.
- **EMERGENCY** — write a deterministic handoff locally without spending another model turn, then block tools.
- **STOPPED** — the budgeted task is over until a new budget is explicitly started.

## Completion invariant

The strongest rule in the project is:

> **A quota floor is a limit, never a target.**

As soon as the task's acceptance criteria are satisfied, the agent stops, even if a large amount of the budget was unused. The remaining allowance is returned to the user; it is never treated as quota that should be spent.

### The budget belongs to one task

A budget covers one instruction and ends by itself when that turn ends: the
`Stop` hook releases it (or, in LAND, saves the handoff). The agent has nothing
to run when it finishes, and a forgotten budget cannot linger and claim landing
reserve from later work. A message you add while the agent is working never ends
the budget, so adding a sentence cannot leave the rest of the task unguarded. If
you press stop, no `Stop` fires, so the budget carries over and ends with the
next turn. A new Landing command starts a fresh budget, while account-level
quota snapshots and measured burn history remain shared so reserve estimates do
not reset.

An agent may still run `landing complete`; it never actually runs. The gate intercepts it and
completes the conversation's own budget, because only the hook knows which
conversation is asking; the command run from a terminal would act on the
terminal's budget instead. The agent sees a denial whose reason confirms the
completion. Completing is the one release an agent may perform, because it ends
spending rather than lifting the floor. It counts only as the bare command,
with nothing chained to it, and only before the landing boundary: past it the
task is out of budget rather than finished, and the handoff is still due.

To end an active budget early, send `/landing cancel` (Codex: `$landing cancel`) as a whole message. The guard stands down, and the session is kept on disk so its burn history remains useful to the next budget.

It has to be sent as an instruction rather than run as a command, because the gate denies the agent its own cancel — an agent must never lift its own floor. The `UserPromptSubmit` hook is the one channel an agent cannot manufacture, so that is where a cancel is honoured. A phrasing that only *resembles* a cancel ("forget the budget") is answered with a pointer to the exact command and changes nothing: guessing here would unguard a user who still believes they are covered.

## Quota windows and plans

A plan can expose several windows at once. A Codex Plus account reports both a five-hour and a weekly window; Claude Code reports `five_hour` and `seven_day`, either of which may be absent. Users should not have to know which windows they have, so **the floor applies to all of them** and work stops at whichever one reaches it first.

Naming a window scopes the floor to that one instead. That is the escape hatch for "I know the five-hour window is low, budget me against the weekly one" — and it is the only thing the optional `weekly`/`5h` token does.

**Landing feasibility is checked on every window regardless**, scoped or not. Being able to write a handoff is a safety property, not a preference: once any window is exhausted the handoff cannot be written, however healthy the budget looks.

Why the floor is not simply pinned to the longest window: it used to be, on the assumption that a five-hour window is a rate limit that refills after a nap while a weekly window is the real budget. A live Codex Plus payload disproved the premise the design rested on — Plus reports *both* windows. With `leave 70` pinned to the weekly window, the five-hour window burned from 94% to 3% remaining while the guard stayed in NORMAL, because 70% of a week is not reachable inside one session. The floor never bound; only the landing reserve eventually did.

If the window a session was scoped to disappears — a plan change, or a host that stops reporting it — the budget migrates to the longest window still available. A window that is *present but unreadable* is not treated as missing: migrating there would move the floor onto a roomier window and report the result as a known quota, so the guard reports unknown quota and conserves instead.

## Trusting the quota reading

Used and remaining are the same number: `30` is identical whether it means 30% spent or 30% left. Polarity is therefore decided by the **field name** and never inferred from the value. A payload whose polarity cannot be established is reported as unreadable rather than guessed.

Unreadable windows are named in `landing status` and the statusline instead of vanishing, because a window that disappears without explanation is also a window that is silently unguarded. When no window can be read at all, the state becomes `CONSERVE` with `QUOTA UNKNOWN` — never `NORMAL`. A reading older than `--max-snapshot-age-seconds` (default 15 minutes) is likewise not treated as current headroom, and is displayed with its age rather than as a live number.

## Burn-rate runway (not task-cost prediction)

Quota Floor deliberately does **not** predict "this task will cost 8%". Task-size forecasts are too dependent on unknown repo complexity, debugging branches, model behavior, cache effects, and provider accounting.

Instead, the dashboard derives a **runway from observed quota movement**:

```text
safe development headroom
        /
observed quota burn rate
        =
approximate runway
```

For example, if 13 percentage points remain available for development and recent observed burn is about 3% per wall-clock hour, the dashboard may show roughly 4.3 hours of recent-pace runway. It also shows a typical rate and a heavy-use (p90) rate.

This estimate is intentionally advisory. It never changes the hard quota floor. If there are not enough real quota-movement samples, the UI says **Learning** rather than inventing a task-cost estimate. Quota resets/increases, duplicate timestamps, and very long idle gaps are excluded from the burn-rate calculation.

```bash
landing status
landing dashboard
# open http://127.0.0.1:8765
```

The dashboard focuses on `CURRENT`, `FLOOR`, `LANDING RESERVE`, `SAFE HEADROOM`, observed burn rate, and runway.

## Dynamic landing reserve

Before there is enough history, the default reserve is 4 percentage points. As quota snapshots arrive, Quota Floor records the observed decrease between snapshots and estimates:

```text
reserve = clamp(
  p90(recent positive per-tool-call burn) * landing_tool_calls * safety_factor,
  min_reserve,
  max_reserve
)
```

Defaults:

- minimum reserve: 3%
- initial/default reserve: 4%
- maximum reserve: 15%
- landing tool calls: 8
- safety factor: 1.5
- conserve warning: 5% before the landing boundary

The calculation is local Python. It does **not** invoke an AI model.

## Handoff contract

In LAND the agent writes no file. Its final reply is the handoff, and the `Stop`
hook saves that reply to `~/.landing/handoffs/<time>-<task>.md` -- outside your
folder, which may not be a project at all. `landing status` shows where the last
one went. The handoff must include:

1. original task / goal;
2. completed and verified work;
3. files changed and intent;
4. validation/tests and results;
5. incomplete or unverified work;
6. important decisions and failed approaches;
7. exact next steps;
8. a resume prompt.

If quota reaches the emergency margin, Quota Floor writes a lower-fidelity but deterministic handoff from local task metadata (and Git state, when the folder is a repository) without another model call. It is saved to the same directory.

## Design principles

1. **Never spend AI tokens deciding whether to spend AI tokens.** Quota acquisition and policy math live outside the model.
2. **Final floor means final floor.** The guard begins landing *before* the user's requested remaining percentage.
3. **Unknown is not 100%.** Missing or stale quota data must never be interpreted as unlimited headroom.
4. **Provider-neutral core.** Claude Code and Codex differ in quota acquisition, not in budget semantics.
5. **Graceful degradation.** Rich model handoff when possible; deterministic Git-state handoff in an emergency.
6. **The gate fails closed.** A hook that crashes is a hook the host ignores, which would leave the session unguarded while the user still believes it is protected. If the budget cannot be evaluated, work is denied and the message says how to stand the guard down. Where no budget was ever requested, an internal error blocks nothing.

## Starting a budget from the terminal

The hooks open budgets from `/landing` invocations. The same budget can be
started directly, which is also how to test with a manual quota value:

```bash
# Leave at least 70% after the task lands
landing start \
  --provider claude \
  --window auto \
  --leave 70 \
  --task "Implement OAuth refresh"

# Spend at most 15 percentage points from the current quota
landing start \
  --provider codex \
  --window auto \
  --spend 15 \
  --task "Refactor the authentication layer"
```

Smoke test the policy without either agent installed:

```bash
landing simulate --start 100 --leave 70 --burn 0.5,0.7,0.6 --step 2
```

Bootstrap a fake Claude snapshot for local testing:

```bash
landing start \
  --provider claude \
  --window five_hour \
  --leave 70 \
  --remaining 90 \
  --task "Demo task"
```
