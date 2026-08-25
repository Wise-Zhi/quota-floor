# Agent Landing

**Give a coding agent a task and a quota budget. It works inside that budget, reserves enough quota to land, writes a resumable technical handoff, and stops.**

> Alpha MVP: Claude Code + OpenAI Codex.

## Why

Coding agents know how to keep working until a task is done. They are much worse at preserving *account quota* for the next task. A fixed "stop at 5%" threshold is also wrong: writing tests, inspecting the diff, and producing a useful handoff consumes quota too.

Agent Landing treats the last part of the budget as a **landing reserve**. A requested percentage is a **floor, never a spending target**: if the task finishes with 91% remaining even though the user asked to preserve 75%, Agent Landing stops at 91%. It never invents extra work to consume the allowance.

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

## Where users actually use it

Landing should feel native to the coding agent. There is no required standalone app. The primary entrypoint is simply **`landing`**:

```text
Claude Code:  /landing leave 70 -- refactor auth and add tests
Codex:        $landing leave 70 -- refactor auth and add tests
Terminal:      landing status
```

Claude Code shows the expected format directly in slash-command autocomplete:

```text
/landing (leave|spend) [weekly|5h] <percent> -- <task>
```

- `leave 70` stops while at least 70% **remains**; `spend 15` spends at most 15
  percentage points. One of the two keywords is **required**: a bare number is
  refused, so that a dropped keyword can never silently mean the opposite.
- `weekly` or `5h` **scopes** the floor to one window. Omit it and the floor
  guards every window your plan exposes, stopping at whichever reaches it first.
- `--` is optional; the first word that is not a keyword, window, or number
  starts the task.
- The settings may come **before or after** the task, whichever reads more
  naturally: `leave 70 -- fix the tests` and `fix the tests leave 70` are the
  same request. A trailing group is only recognized when it is complete (a
  keyword plus a number), so a number or the word `leave` occurring inside the
  task text is never mistaken for settings.

For example: `/landing leave 75 -- fix the checkout tests`. In Codex, selecting
`$landing` provides the editable starter `Use $landing leave 75 -- <task>`.

Invocations are parsed by `landing invoke`, not by the model, so both hosts agree
and a typo is refused instead of silently starting an unguarded task.

Users interact with it in three places:

1. **Inside the existing Claude Code or Codex conversation** — invoke `/landing` in Claude Code or `$landing` in Codex. Natural-language budget requests can still be supported, but the explicit command is the discoverable default.
2. **In the terminal statusline** — keep a small always-visible budget indicator such as `AL | W 84->70% | safe +11% | SAFE`.
3. **On demand in the same terminal** — run `landing status` for details or `landing status --compact` for the one-line view.

The local web dashboard is optional diagnostics/history only. It should never be required for normal use and should not pop up automatically.

A future installer should print this once after setup:

```text
Landing is ready.
Use it where you already work:
  Claude Code:  /landing leave 70 -- <task>
  Codex:        $landing leave 70 -- <task>
  Quick check:  landing status --compact
  Full status:  landing status
```

## User-facing budget syntax

Two equivalent budget modes are supported:

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

The intended AI UX is explicit and short: use `/landing` in Claude Code or `$landing` in Codex. For example: `/landing leave 70 -- implement OAuth refresh`. The skill translates that into the provider-neutral budget command before implementation.

## States

- **NORMAL** — work normally.
- **CONSERVE** — quota is getting close; finish the bounded objective and avoid new workstreams.
- **LAND** — block new implementation work, permit minimal read-only inspection, require `AGENT_HANDOFF.md`, then stop.
- **EMERGENCY** — write a deterministic handoff locally without spending another model turn, then block tools.
- **STOPPED** — the budgeted task is over until a new budget is explicitly started.

## Completion invariant

The strongest rule in the project is:

> **A quota floor is a limit, never a target.**

As soon as the task's acceptance criteria are satisfied, the agent runs `landing complete`. The session becomes `STOPPED` immediately, even if a large amount of the budget was unused. The remaining allowance is returned to the user; it is never treated as quota that should be spent.

### The budget outlives the task

A budget belongs to the user, not to one task. Finishing a task must not quietly drop the guard for whatever is asked next, so a completed session starts a **new segment** under the same floor rather than being released.

Only a *new user instruction* may start that segment. Both Claude Code and Codex expose a `UserPromptSubmit` hook for this (verified on codex-cli 0.149.0-alpha.4.3); `landing resume` is the manual fallback for hosts that do not. A tool call can never do it, because a tool call cannot prove a user asked for anything — allowing that would let the agent work straight past `landing complete`.

A session that stopped because quota actually ran out (`landed` or `emergency`) is **not** renewable. Renewing there would defeat the floor.

To end the contract itself rather than one task, run `landing cancel`. The guard stands down, later instructions do not restart the floor, and the session is kept on disk so its burn history remains useful to the next budget.

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

Agent Landing deliberately does **not** predict "this task will cost 8%". Task-size forecasts are too dependent on unknown repo complexity, debugging branches, model behavior, cache effects, and provider accounting.

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

Before there is enough history, the default reserve is 4 percentage points. As quota snapshots arrive, Agent Landing records the observed decrease between snapshots and estimates:

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

## Claude Code

Claude Code's status-line payload exposes the base `five_hour` and `seven_day` usage windows. Configure `landing claude-statusline` so the runtime quota is persisted locally, then use the Pre/PostToolUse gates.

See [`examples/claude/settings.json`](examples/claude/settings.json).

Important current limitation: Claude Code may have model-scoped weekly limits that are visible in `/usage` but are not always projected into statusLine. Agent Landing never treats an unknown limit as 100% available.

## Codex

Codex exposes ChatGPT quota through the stable app-server method `account/rateLimits/read`. Agent Landing queries this runtime RPC directly; quota math does not require a model turn.

See [`examples/codex/config.toml`](examples/codex/config.toml).

The Codex hook refreshes a stale quota snapshot before evaluating a tool call. Current Codex hook support is still evolving across CLI/Desktop builds, so v0.1 targets the documented/implemented hook schema and treats runtime hook regressions as a platform limitation rather than fabricating quota state.

## Install for development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
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

## Handoff contract

The model-written handoff must include:

1. original task / goal;
2. completed and verified work;
3. files changed and intent;
4. validation/tests and results;
5. incomplete or unverified work;
6. important decisions and failed approaches;
7. exact next steps;
8. a resume prompt.

If quota reaches the emergency margin, Agent Landing writes a lower-fidelity but deterministic handoff from local task metadata and Git state without another model call.

## Design principles

1. **Never spend AI tokens deciding whether to spend AI tokens.** Quota acquisition and policy math live outside the model.
2. **Final floor means final floor.** The guard begins landing *before* the user's requested remaining percentage.
3. **Unknown is not 100%.** Missing or stale quota data must never be interpreted as unlimited headroom.
4. **Provider-neutral core.** Claude Code and Codex differ in quota acquisition, not in budget semantics.
5. **Graceful degradation.** Rich model handoff when possible; deterministic Git-state handoff in an emergency.

## Technical documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data flow, state machine, invariants, and provider boundary.
- [`docs/TESTING.md`](docs/TESTING.md) — unit, simulation, Claude Code, Codex, and dashboard test plan.
- [`CLAUDE.md`](CLAUDE.md) — repository-level instructions for Claude Code reviewers/testers.

## v0.1 limitations / next work

- Add an installer that merges hook config safely instead of requiring manual config snippets.
- Validate against real Claude Code and Codex installations on macOS/Linux/Windows.
- Add task-history/burn-rate charts and a compact cross-provider statusline mode.
- Add model-scoped Claude limits when/if the runtime exposes them to statusLine.
- Make landing-safe test commands configurable instead of using a minimal built-in allowlist.
- Add a local event log so the handoff can include commands/tests even when the model cannot spend a final turn.
- Package as a Claude/Codex installable skill/plugin after the runtime integration is proven.

### Codex enforcement caveat

Codex's hook schema supports `PreToolUse` allow/deny decisions, but current builds have open bug reports where a deny can fail to stop some `apply_patch` writes. Agent Landing therefore treats Codex v0.1 enforcement as best-effort until the host fixes that path. The quota policy and telemetry remain useful, but the README does not claim a stronger guarantee than the runtime can provide.

## License

MIT
