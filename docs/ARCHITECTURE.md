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
- `QuotaSnapshot`: a timestamped set of quota windows from one provider, plus `unreadable_windows` describing any window the provider reported but whose value could not be trusted. `longest_window()` is only used to re-pin a budget whose window disappeared; it is not where the floor lives.
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

Persists local runtime state:

- `session.json`
- `latest_snapshot.json`
- `snapshots.jsonl`

`resolve_state_root()` puts these in `~/.landing/` by default. Quota is
account-wide, so the budget is too: state used to live in `<cwd>/.landing/`,
which meant a floor set in one checkout stopped guarding the moment the agent
worked in another against the same quota, and every clone restarted its burn
history and therefore its landing reserve. `--state-scope project` (or
`LANDING_STATE_SCOPE=project`) is the explicit opt-out; `LANDING_HOME` /
`AGENT_LANDING_HOME` still override both. `adopt_project_state()` carries a
budget left in an older project-scoped root up to the user-level one, copying
rather than moving, and never over a budget already in force.

Because one budget spans checkouts, segment renewal re-points `session.cwd` at
the directory the new instruction came from — an emergency handoff has to land
where the work actually is.

Writes go through `os.replace` so the file is swapped in one step. The session is
rewritten on nearly every tool call, which makes a half-written file reachable
rather than theoretical — and a half-written session is one the gate can no
longer read.

`lock()` serializes the read-modify-write of the session across concurrent hook
processes. Hosts issue tool calls in parallel and each one runs the gate
separately; unserialized, the calls overwrite each other's burn accounting.
Measured at 40-way concurrency, 1 of 40 tool calls was counted, which biases the
landing reserve *downwards* — the unsafe direction. The lock degrades to
proceeding unlocked after a short timeout, because stalling a user's tool call is
worse than a lost update, and atomic writes keep the file readable either way.

A session file that exists but will not parse raises `CorruptStateError`, which
is deliberately not the same as no session: it means a budget *was* requested and
its terms are now unknown. `quarantine_session()` moves such a file aside, which
is how `landing cancel` stands the guard down on exactly the state that caused a
denial. An unreadable *snapshot* has a safe reading instead — no reading at all,
which the policy already handles as CONSERVE — so it degrades to `None`.

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

1. `window_pressure()` on every window, each judged against the floor that applies to it. The floor is a limit on *all* windows a plan exposes unless the user scoped it to one, because a plan can expose several at once and the shortest is usually what binds inside a session.
2. Any window whose quota could not be read. The floor cannot be checked there, and an unreadable window is not an absent one, so it forces `CONSERVE` without claiming the window is exhausted.
3. Snapshot staleness, which can only tighten `NORMAL` into `CONSERVE`.

`window_floor()` resolves the floor per window. `leave N` is an absolute level and reads the same everywhere; `spend N` is a delta, so it is measured from where *that* window stood when the budget started (`started_remaining_by_window`) — the windows burn at very different rates. A window scoped out by an explicit `--window` gets a floor of `0.0`, which reduces `window_pressure()` to a pure landing-feasibility check: the user owns the floor, but being able to write a handoff is a safety property and is not opt-out-able.

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

`resolve_budget_window()` migrates a session to the longest available window if the one it was pinned to disappears, rather than silently ceasing to guard. A window that is *present but unreadable* is explicitly not treated as missing: migrating there would move the budget onto a roomier window and report the result as a known quota, so the session stays pinned and the decision reports unknown quota instead.

The gate never fails open. A hook that raises exits non-zero, and Claude Code
treats every exit code other than 2 as a non-blocking error — the tool then runs
anyway — so a crash inside the gate silently removes the guard while the user
still believes they are protected. `pre_tool()` answers unreadable state with a
denial, and `gate_failure()` converts any other internal error into one as well.
Where no session file exists there is no mandate to enforce, so a failure there
stays out of the way exactly as the healthy path does. `post_tool()` and
`user_prompt()` fail *safe* rather than closed: they can only end or renew a
budget, so doing nothing leaves the guard running.

`post_tool()` marks the task stopped once the model-written handoff succeeds,
with `stop_reason="landed"`. Success means the file is on disk and non-empty, not
merely that the write tool reported success: `landed` is terminal and not
renewable, so recording it for a failed write would end the budget with no
handoff at all — the exact outcome the landing reserve exists to prevent.

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

`claude-statusline --wrap` shares Claude Code's single statusLine slot. Claude
reports `rate_limits` there and nowhere else, so refusing to overwrite an
existing status line locked out exactly the users who already run one. Landing
ingests quota from the payload, then runs the wrapped command with that same
payload and prints its output. The wrapped command is stored in Landing's own
status line command rather than in separate state, so it cannot go stale and
`landing uninstall` can restore it from the config alone. A failing wrapped
command falls back to Landing's own line: an empty status bar would read as
Landing having broken the user's.

The pre-flight check in `start` asks `window_floor()` and `window_pressure()` the
same questions the gate asks on the first tool call, so a budget is never refused
on terms the guard would not have enforced. Comparing every window against the
single absolute floor derived from the budget window was wrong for `spend`, which
is a per-window delta.

## The budget contract outlives the task

A budget belongs to the user, not to one task. Finishing a task must not quietly drop the guard for whatever is asked next, so a completed session starts a **new segment** under the same floor rather than being released.

Only a *new user instruction* may start that segment: the `UserPromptSubmit` hook on Claude Code and on Codex, `landing resume` on hosts without one. A tool call can never do it, because a tool call cannot prove a user asked for anything — allowing that would let the agent work straight past `landing complete`.

A session that stopped because quota actually ran out (`landed` or `emergency`) is **not** renewable.

`cancel` is the one stop reason that stands the guard down completely: `evaluate_current` reports nothing for it, so tool use is unrestricted. Every other stopped state still gates. Without this the budget had no exit at all — renewal made `complete` non-terminal, and the only escape was deleting `session.json` by hand.

### Cancelling has to be reachable from the chat

The gate denies `landing cancel` as a tool call, and must: an agent that can lift its own floor has no floor. But that left the contract with an exit only a terminal could reach. Demonstrated on 2026-08-27: a budget armed to test one host also gated the agent on another, the agent could not release itself by design, and the user was away from a shell — so nobody could stand the guard down.

`user_prompt()` therefore honours a cancel directly. It is the one channel an agent cannot manufacture, and it already receives the user's own words, so it needs no token or later authorization — it simply performs the cancel before the turn begins.

The rule that keeps this safe is **recognize loosely, act strictly**:

- `is_cancel_invocation()` matches the *whole* prompt against `/landing cancel`, `$landing cancel`, or `landing cancel`, and only then cancels. Substring matching would make "why does `/landing cancel` not work?" silently unguard the user.
- `suggests_cancel()` matches looser phrasings ("forget the budget", "取消预算") and only ever returns a hint naming the exact command. It never changes state, because acting on a guess unguards a user who still believes they are covered — the failure this project ranks below having no guard at all.

Every denial the gate emits carries `CANCEL_HINT`, so the message that blocks a user also tells them the way out. `parse_invocation()` accepts the bare verb too, so hosts without a `UserPromptSubmit` hook reach the same behaviour through `landing invoke`.

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
11. The gate fails closed. A gate that cannot evaluate the budget denies rather than crashing, because a crashing hook is a hook the host ignores.
12. Damaged local state is never read as "never guarded", and `cancel` always works on it.
13. The contract always has an exit the user can reach without a terminal, and the agent still cannot take it.
13. The budget follows the account, not the checkout, because the quota does.
14. Landing shares the host's status line; it never takes a slot the user was already using.

## Provider boundary

The provider interface is intentionally small: convert provider-specific quota data into `QuotaSnapshot`. Policy, runway, handoff behavior, and task semantics are provider-neutral.

## Current alpha limitations

- Claude `rate_limits` requires a Claude.ai subscription and appears only after the first API response of a session; API-key users cannot be guarded.
- Claude model-specific limits may not all be available through statusLine.
- **Some Claude Code surfaces never invoke the status line at all.** Verified on Windows on 2026-08-27: the `PreToolUse`, `PostToolUse` and `UserPromptSubmit` hooks all ran, while the configured `statusLine` command was never called once across a full session. Because the status line is Landing's only Claude quota source, the Claude guard has no quota on such a surface and stays in `CONSERVE` / `QUOTA UNKNOWN` — every other check reports a healthy install. `landing doctor` reports this as `quota_source: pending`; see below.
- Codex hook enforcement depends on host behavior; some current builds have reported deny-path regressions for writes such as `apply_patch`.
- Codex exposes `UserPromptSubmit` as of codex-cli 0.149.0-alpha.4.3, verified via `hooks/list` on its app-server. On builds without it, segment renewal is manual via `landing resume`.
- Burn-rate runway uses wall-clock intervals and excludes long idle gaps; active-compute time is not yet measured directly.
- Installer/config merging and real cross-platform integration testing are still pending.
