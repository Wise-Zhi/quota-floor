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

Expected current baseline: all repository tests pass (149 at time of writing).

Also run:

```bash
python -m compileall -q src
landing --help
landing simulate --start 100 --leave 70 --burn 0.5,0.7,0.6 --step 2
```

> The suite clears `LANDING_HOME`/`AGENT_LANDING_HOME` via `tests/conftest.py`. Without that, anyone who actually uses Agent Landing would have their live `.landing/` written to by the tests, and stores that are supposed to be independent would collapse into one directory.

## High-priority behavioral tests

### 1. Floor is not a target

Start a fake task with ample quota:

```bash
landing invoke "leave 75 -- early completion test" --provider claude
```

Then complete it before reaching the floor:

```bash
landing complete
landing status
```

Verify that the task is `STOPPED` and that no logic attempts to spend down to 75%.

Relevant regression tests: `tests/test_hooks.py::test_task_complete_stops_even_far_above_floor`, and `tests/test_budget_contract.py::test_agent_cannot_self_renew_after_completing`.

### 2. The budget survives the task, but only a user can renew it

Verify that after `landing complete`:

- a tool call is still denied — the agent cannot renew its own segment;
- a `UserPromptSubmit` hook call starts segment 2 under the **same** floor;
- a session stopped as `landed` or `emergency` is **not** renewable.

Relevant tests: `tests/test_budget_contract.py`.

Verify too that `landing cancel` ends the contract: tool use becomes unrestricted, a later user prompt does not revive it, and the session stays on disk. Relevant tests: `tests/test_cancel.py`.

### 3. LAND blocks new implementation but allows handoff

Construct a state whose remaining quota is at the development floor. Verify:

- ordinary writes are denied;
- read-only inspection remains available;
- the configured `AGENT_HANDOFF.md` write is allowed;
- after the handoff write, the session becomes `STOPPED`.

Verify too that the command allowlist cannot be escaped: `git status && <any write>`, `git diff > out.txt`, and `git status $(...)` must all be denied while plain `git status` is allowed.

Relevant tests: `tests/test_hooks.py`, `tests/test_landing_gate.py`.

### 4. Emergency fallback does not require a model turn

Move remaining quota into the emergency margin and invoke a blocked write. Verify that:

- the write is denied;
- `AGENT_HANDOFF.md` is created locally;
- the task state becomes `STOPPED`.

Relevant test: `test_emergency_writes_fallback`.

### 5. Reserve does not drift with snapshot frequency

The landing reserve is priced per **tool call**, not per snapshot interval. Verify that the same work sampled coarsely (one snapshot per six calls) and finely (one per call) produces the same per-call rate and therefore the same reserve.

Relevant tests: `tests/test_burn_sampling.py`.

### 6. Every window is checked, and plans differ

Verify that:

- with both windows present, the floor lands on the weekly one;
- with only a five-hour window present (a plan without a weekly limit), that window becomes the budget;
- a healthy weekly window does **not** mask an exhausted five-hour window — landing is forced by whichever runs out first;
- if the pinned window disappears, the budget migrates rather than silently ceasing to guard.

Relevant tests: `tests/test_multi_window.py`.

### 7. Unknown and stale quota are not headroom

Verify that:

- a snapshot older than `max_snapshot_age_seconds` downgrades `NORMAL` to `CONSERVE` and is displayed with its age;
- staleness only ever tightens, never relaxes;
- when no window can be read, the state is `CONSERVE` with `quota_known == False`, never `NORMAL`;
- unreadable windows are named in `landing status` and the statusline rather than vanishing.

Relevant tests: `tests/test_landing_gate.py`, `tests/test_quota_polarity.py`, `tests/test_multi_window.py`.

### 8. Quota polarity is never inferred from a value

`30` is identical whether it means 30% used or 30% remaining. Verify that polarity comes from the field name, that a contradictory used/remaining pair is rejected, and that a missing field yields unknown rather than 100%.

Relevant tests: `tests/test_quota_polarity.py`.

### 9. Invocations are parsed in code and refused on ambiguity

Verify that:

- a bare number is **refused** — `leave` or `spend` is required;
- a refusal never creates a session;
- suggestions are offered as text and are themselves valid syntax;
- every example in the documentation actually parses.

Relevant tests: `tests/test_invocation.py`.

### 10. Runway is telemetry, not task-cost prediction

Verify that:

- one snapshot is not enough to fabricate a forecast;
- real quota movement produces `%/hour` and runway;
- quota resets/increases are ignored;
- dashboard labels describe observed burn-rate runway rather than total task cost.

Relevant tests: `tests/test_runway.py` and `tests/test_dashboard.py`.

## Claude Code integration test

Use `examples/claude/settings.json` as the integration reference, and read `examples/claude/README.md` first. Merge the settings into a disposable Claude Code configuration rather than overwriting an existing one.

The statusLine contract is pinned by `tests/test_claude_statusline_contract.py`, which parses the documented payload verbatim. If Claude Code changes that schema, that test should fail loudly rather than the guard quietly ceasing to guard.

Validation goals:

1. Claude statusLine payload is ingested and stored under `.landing/`.
2. The stored numbers match what `/usage` reports.
3. `landing status` shows the selected quota window.
4. PreToolUse receives `CONSERVE`/`LAND` context at the correct boundary.
5. In `LAND`, new writes are denied while handoff output remains permitted.
6. `UserPromptSubmit` renews a completed segment under the same floor.
7. Early completion remains stopped even if substantial quota is unused.

Known host behaviors to account for:

- `rate_limits` appears only for Claude.ai subscriptions, and only after the first API response of a session. A budget requested as the very first action in a session may legitimately find no snapshot.
- `five_hour` and `seven_day` may each be absent independently.
- The pip-generated `landing` executable is unsigned and some machines block it via Windows Application Control. The symptom is a hook that never runs — which does **not** block anything, so the session is silently unguarded. Prefer `python -m agent_landing.cli` with absolute paths, and confirm enforcement rather than assuming it.

Do not intentionally burn account quota just to hit a threshold. Prefer a floor above the current quota (`landing invoke "leave 99 -- wiring check"`), which enters `LAND` immediately without spending anything.

## Codex integration test

Validation goals:

1. `landing codex-snapshot --json` returns quota without a model turn.
2. The requested `five_hour`/`seven_day` window maps correctly, and `usedPercent` is read as consumed quota.
3. Stale quota is refreshed before a Codex PreToolUse decision.
4. The host actually honors deny decisions for the tool paths under test.
5. Segment renewal works: `UserPromptSubmit` where the build has it (0.149+), `landing resume` otherwise.

Because current Codex builds may differ in hook enforcement, record the exact Codex version and whether `apply_patch`/write denial was actually enforced.

### Capturing a fresh rate-limit payload

`tests/test_codex_rate_limits_contract.py` pins a payload captured from
codex-cli 0.149.0-alpha.4.3. Re-capture it when the Codex version moves, since
the field names are an external contract that no amount of unit testing can
verify:

```bash
printf '%s
' '{"method":"initialize","id":1,"params":{"clientInfo":{"name":"probe","title":"probe","version":"0.1.0"}}}' '{"method":"initialized"}' '{"method":"account/rateLimits/read","id":2}' | codex app-server
```

Keep the input channel open for a second or two; closing stdin immediately makes
`app-server` drop the request before answering. Responses are matched by `id`
because unrelated notifications are interleaved into the stream.

What the last capture established, and what to re-check:

- A **Plus** account reports both windows (`primary` 300 min, `secondary` 10080 min). Do not assume a plan implies a single window — that assumption is what let a floor bind to the window that never binds.
- The percentage key is `usedPercent`, camelCase and *consumed*. There is no remaining-percent key.
- `resetsAt` is integer Unix seconds.
- `rateLimitReachedType`, `spendControlReached`, `individualLimit` and `credits` are reported but not yet consumed by Agent Landing.

If the percentage key is ever absent or renamed, the window is recorded as unreadable rather than dropped, which forces `CONSERVE`. Verify that path rather than assuming the parse succeeded.

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
