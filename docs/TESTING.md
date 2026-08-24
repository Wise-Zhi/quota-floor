# Testing Agent Landing

This document is intended for both humans and coding agents reviewing the alpha MVP.

## Baseline

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -e .
pytest -q
```

Expected current baseline: all repository tests pass.

Also run:

```bash
python -m compileall -q src
landing --help
landing simulate --start 100 --preserve 70 --burn 0.5,0.7,0.6 --step 2
```

## High-priority behavioral tests

### 1. Floor is not a target

Start a fake task with ample quota:

```bash
landing start \
  --provider claude \
  --window seven_day \
  --preserve 75 \
  --remaining 95 \
  --task "early completion test"
```

Then complete it before reaching the floor:

```bash
landing complete
landing status
```

Verify that the task is `STOPPED` and that no logic attempts to spend down to 75%.

Relevant regression test: `tests/test_hooks.py::test_task_complete_stops_even_far_above_floor`.

### 2. LAND blocks new implementation but allows handoff

Use the hook unit tests or construct a state whose remaining quota is at the development floor. Verify:

- ordinary writes are denied;
- read-only inspection remains available;
- the configured `AGENT_HANDOFF.md` write is allowed;
- after the handoff write, the session becomes `STOPPED`.

Relevant tests:

- `test_land_blocks_new_write_but_allows_handoff`
- `test_post_handoff_stops_session`

### 3. Emergency fallback does not require a model turn

Move remaining quota into the emergency margin and invoke a blocked write. Verify that:

- the write is denied;
- `AGENT_HANDOFF.md` is created locally;
- the task state becomes `STOPPED`.

Relevant test: `test_emergency_writes_fallback`.

### 4. Dynamic reserve grows under heavier observed burn

Verify that multiple larger burn samples increase the landing reserve and therefore move the development floor upward.

Relevant test: `tests/test_policy.py::test_dynamic_reserve_grows_with_burn_rate`.

### 5. Runway is telemetry, not task-cost prediction

Verify that:

- one snapshot is not enough to fabricate a forecast;
- real quota movement produces `%/hour` and runway;
- quota resets/increases are ignored;
- dashboard labels describe observed burn-rate runway rather than total task cost.

Relevant tests: `tests/test_runway.py` and `tests/test_dashboard.py`.

## Claude Code integration test

Use `examples/claude/settings.json` as the integration reference. Before testing against a real account, inspect the settings and merge them into a disposable Claude Code configuration rather than overwriting an existing one.

Validation goals:

1. Claude statusLine payload is ingested and stored under `.landing/`.
2. `landing status` shows the selected quota window.
3. PreToolUse receives `CONSERVE`/`LAND` context at the correct boundary.
4. In `LAND`, new writes are denied while handoff output remains permitted.
5. Early completion remains stopped even if substantial quota is unused.

Do not intentionally burn account quota just to hit a threshold. Prefer a manual/fake snapshot or a very high preserve floor for integration testing.

## Codex integration test

Validation goals:

1. `landing codex-snapshot --json` returns quota without a model turn.
2. The requested `five_hour`/`seven_day` window maps correctly.
3. Stale quota is refreshed before a Codex PreToolUse decision.
4. The host actually honors deny decisions for the tool paths under test.

Because current Codex builds may differ in hook enforcement, record the exact Codex version and whether `apply_patch`/write denial was actually enforced.

## Dashboard test

```bash
landing dashboard
```

Open `http://127.0.0.1:8765` and verify:

- Current quota and floor are visually distinct.
- Floor copy explicitly says it is never a spending target.
- Runway reports `Learning` until real movement exists.
- Runway changes only after new real snapshots.
- No UI text claims to predict total task cost.

## What a reviewer should report

For every failure, record:

- OS and Python version;
- Claude Code or Codex version if applicable;
- exact command or hook payload;
- expected result;
- actual result;
- `.landing/session.json` state (remove sensitive task text if needed);
- minimal reproduction;
- whether the bug is core policy, provider acquisition, or host hook enforcement.

## Non-goals for testing

Do not evaluate the project by deliberately consuming quota to the user's requested floor. The floor is a safety boundary. Tests should use simulated/manual quota wherever possible.
