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
- `QuotaSnapshot`: a timestamped set of quota windows from one provider.
- `LandingConfig`: reserve and state-machine parameters.
- `TaskSession`: persisted per-task budget state.
- `PolicyDecision`: the evaluated state for the current snapshot.

### `providers.py`

Quota acquisition adapters.

- Claude Code: parses `statusLine` JSON and extracts `rate_limits.*.used_percentage`.
- Codex: starts `codex app-server` and calls `account/rateLimits/read` over line-delimited JSON-RPC.

Quota acquisition does not require a model inference turn.

### `state.py`

Persists local runtime state under `.landing/` by default:

- `session.json`
- `latest_snapshot.json`
- `snapshots.jsonl`

The directory can be overridden with `AGENT_LANDING_HOME`.

### `policy.py`

Computes the hard landing decision.

The core calculation is:

```text
development_floor = user_floor + landing_reserve
safe_headroom = current_remaining - development_floor
```

The reserve begins at a default value and adapts from observed positive quota drops. It uses recent p90/median burn samples, clamps the result, and never depends on runway prediction.

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
- `STOPPED`: denies further work until a new budget is started.

`post_tool()` marks the task stopped once the model-written handoff succeeds.

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
- starting a budgeted task;
- hook integration;
- status and dashboard;
- early completion;
- deterministic fallback handoff;
- local state-machine simulation.

## Core invariants

1. A quota floor is a limit, never a target.
2. `complete` stops immediately even far above the floor.
3. Unknown quota is never fabricated as 100% available.
4. Runway telemetry cannot change enforcement decisions.
5. The model is not asked to calculate whether the model should spend more quota.
6. Landing starts before the user floor so handoff work has reserved budget.
7. Emergency handoff has a no-model-call path.

## Provider boundary

The provider interface is intentionally small: convert provider-specific quota data into `QuotaSnapshot`. Policy, runway, handoff behavior, and task semantics are provider-neutral.

## Current alpha limitations

- Claude model-specific limits may not all be available through statusLine.
- Codex hook enforcement depends on host behavior; some current builds have reported deny-path regressions for writes such as `apply_patch`.
- Burn-rate runway uses wall-clock intervals and excludes long idle gaps; active-compute time is not yet measured directly.
- Installer/config merging and real cross-platform integration testing are still pending.
