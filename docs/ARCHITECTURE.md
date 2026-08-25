# Agent Landing Architecture

## Purpose

Agent Landing is a provider-neutral quota budget guard for coding agents. A user can give a task a quota constraint such as "leave at least 75% weekly quota". The guard monitors real provider quota outside the model, reserves enough quota to land safely, blocks new implementation work at the landing boundary, writes a resumable handoff, and stops.

The requested percentage is a **floor, not a spending target**. If a task finishes while 91% remains and the floor is 75%, the task stops at 91%.

## Safety model

Two paths are deliberately separate:

1. **Enforcement path**: real quota snapshot -> landing reserve -> state machine -> tool gate.
2. **Telemetry path**: quota history -> observed burn rate -> runway shown to the user.

Runway is advisory only. It must never relax the user's floor or authorize additional work.

## Main components

### `models.py`

Defines the provider-neutral data model:

- `QuotaWindow`: one provider quota window, represented as used percent plus reset metadata.
- `QuotaSnapshot`: a timestamped set of quota windows from one provider, plus `unreadable_windows` describing any window the provider reported but whose value could not be trusted. `longest_window()` returns the budget window.
- `window_minutes()`: ranks windows by duration. A five-hour window is a rate limit that refills; a weekly window is the actual budget.
- `LandingConfig`: reserve and state-machine parameters, including `landing_tool_calls` and `max_snapshot_age_seconds`. `from_dict` ignores unknown keys so configs written by older versions still load.
- `TaskSession`: persisted per-task budget state, including `segment_count`, `previous_tasks`, and `tool_calls_since_snapshot`.
- `PolicyDecision`: the evaluated state, carrying `quota_known`, `stale`, `snapshot_age_seconds`, and `limiting_window`. `describe_remaining()` refuses to present an untrustworthy reading as a current number.

### `invocation.py`

Parses `/landing` and `$landing` invocations deterministically, outside the model.

Parsing used to happen inside the model, which made it nondeterministic, cost tokens, and let a typo fail *silently* into the wrong floor. Both hosts now call one function, so they cannot disagree about what an invocation means.

The syntax is `(leave|spend) [weekly|5h] <percent> -- <task>`. One of `leave`/`spend` is **required**: a bare number is refused rather than defaulted, because with a default, dropping the keyword from `spend 15` would leave `15` as a still-legal invocation meaning "leave 15" — authorizing 76 points of spend instead of 15. The governing rule is that *the absence of a marker must not itself be a valid, dangerous meaning*.

Anything ambiguous is refused, never guessed, and a refusal never creates a session. Suggestions are offered as text only and are themselves valid syntax.

### `providers.py`

Quota acquisition adapters.

- Claude Code: parses `statusLine` JSON and reads `rate_limits.*`. Per Claude Code's documentation, `rate_limits` appears only for Claude.ai subscriptions and only after the first API response of a session, and `five_hour`/`seven_day` may each be absent independently.
- Codex: starts `codex app-server` and calls `account/rateLimits/read` over line-delimited JSON-RPC.

`read_quota_percent()` normalizes every payload to *used* percent. Polarity is decided by the **field name** and never inferred from the value, because `30` is identical whether it means 30% spent or 30% left. A payload whose polarity cannot be established, or whose used/remaining pair does not sum to 100, is reported as unreadable rather than guessed.

Quota acquisition does not require a model inference turn.

### `state.py`

Persists local runtime state under `.landing/` by default:

- `session.json`
- `latest_snapshot.json`
- `snapshots.jsonl`

The directory can be overridden with `LANDING_HOME` or `AGENT_LANDING_HOME`.

`save_snapshot` also collects burn samples, normalized to **quota burned per tool call**. Sampling per snapshot interval instead would make the landing reserve drift with however often the host happens to refresh the statusLine. Movement too small to attribute keeps accumulating against the same baseline rather than being discarded, and a quota reset restarts accounting instead of producing a garbage sample.

### `policy.py`

Computes the hard landing decision.

The core calculation is:

```text
development_floor = user_floor + landing_reserve
safe_headroom = current_remaining - development_floor
```

The reserve is priced in tool calls — landing costs reading state, checking git, and writing the handoff — so the samples and the multiplier share one unit.

Three independent signals are combined by taking the **strictest**, never the most permissive:

1. The budget window against the user's floor.
2. `landing_feasibility()` on every *other* window. A reserve held on the budget window is worthless if a different window cuts the agent off first: once any window is exhausted the handoff cannot be written, however healthy the budget looks.
3. Snapshot staleness, which can only tighten `NORMAL` into `CONSERVE`.

`unknown_quota_decision()` is used when no reading can be trusted at all. It yields `CONSERVE`, never `NORMAL`.

State order:

- `NORMAL`
- `CONSERVE`
- `LAND`
- `EMERGENCY`
- `STOPPED`

### `hooks.py`

Implements the agent execution gate.

- `NORMAL`: no intervention.
- `CONSERVE`: injects context once; avoids new large workstreams.
- `LAND`: denies new implementation writes, allows minimal inspection/testing and the configured handoff write.
- `EMERGENCY`: writes a deterministic fallback handoff without a model call, then stops.
- `STOPPED`: denies further work until a new budget or segment is started.

The LAND command allowlist rejects any command carrying shell control characters *before* consulting the allowlist, and then requires a full match. A prefix match alone would let `git status && <any write>` through.

`resolve_budget_window()` migrates a session to the longest available window if the one it was pinned to disappears, rather than silently ceasing to guard.

`post_tool()` marks the task stopped once the model-written handoff succeeds, with `stop_reason="landed"`.

`user_prompt()` is the only place a completed segment may be renewed — see below.

### `handoff.py`

Provides both:

- a model-facing landing prompt for a rich `AGENT_HANDOFF.md`;
- a deterministic local fallback when quota is too low for another model turn.

### `runway.py`

Provides user-facing telemetry only.

It estimates recent/typical/heavy quota burn in `% per wall-clock hour` from real quota snapshots. It ignores resets, increases, duplicate timestamps, microscopic changes, and very long idle gaps. With enough samples it reports an approximate runway:

```text
safe_headroom / observed_burn_rate = approximate_runway
```

If evidence is insufficient, the estimate reports `Learning`/unavailable instead of guessing task cost.

### `dashboard.py`

Serves the local read-only dashboard. It displays current remaining quota, floor, reserve, safe headroom, observed burn rate, runway, trend, and state.

### `cli.py`

Entry point for:

- provider snapshot ingestion;
- `invoke` — parse a raw invocation and start the budget, refusing on any ambiguity;
- starting a budgeted task;
- hook integration (`hook-pre-tool`, `hook-post-tool`, `hook-user-prompt`);
- `resume` — manual segment renewal for hosts without `UserPromptSubmit`;
- status and dashboard;
- early completion, and `cancel` to end the contract entirely;
- deterministic fallback handoff;
- local state-machine simulation.

## The budget contract outlives the task

A budget belongs to the user, not to one task. Finishing a task must not quietly drop the guard for whatever is asked next, so a completed session starts a **new segment** under the same floor rather than being released.

Only a *new user instruction* may start that segment: `UserPromptSubmit` on Claude Code, `landing resume` elsewhere. A tool call can never do it, because a tool call cannot prove a user asked for anything — allowing that would let the agent work straight past `landing complete`.

A session that stopped because quota actually ran out (`landed` or `emergency`) is **not** renewable.

`cancel` is the one stop reason that stands the guard down completely: `evaluate_current` reports nothing for it, so tool use is unrestricted. Every other stopped state still gates. Without this the budget had no exit at all — renewal made `complete` non-terminal, and the only escape was deleting `session.json` by hand.

## Core invariants

1. A quota floor is a limit, never a target.
2. `complete` stops immediately even far above the floor, and the agent cannot renew its own segment.
3. Unknown quota is never fabricated as 100% available, and neither is stale quota.
4. Runway telemetry cannot change enforcement decisions.
5. The model is not asked to calculate whether the model should spend more quota, nor to parse the invocation.
6. Landing starts before the user floor so handoff work has reserved budget.
7. Landing must remain possible on every quota window, not only the budgeted one.
8. Emergency handoff has a no-model-call path.
9. Quota polarity is read from field names, never inferred from values.
10. An ambiguous invocation is refused; it never silently starts an unguarded task.

## Provider boundary

The provider interface is intentionally small: convert provider-specific quota data into `QuotaSnapshot`. Policy, runway, handoff behavior, and task semantics are provider-neutral.

## Current alpha limitations

- Claude `rate_limits` requires a Claude.ai subscription and appears only after the first API response of a session; API-key users cannot be guarded.
- Claude model-specific limits may not all be available through statusLine.
- Codex hook enforcement depends on host behavior; some current builds have reported deny-path regressions for writes such as `apply_patch`.
- Codex has no `UserPromptSubmit` equivalent, so segment renewal there is manual.
- Burn-rate runway uses wall-clock intervals and excludes long idle gaps; active-compute time is not yet measured directly.
- Installer/config merging and real cross-platform integration testing are still pending.
