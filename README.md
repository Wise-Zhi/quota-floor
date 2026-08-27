# Agent Landing

**Give a coding agent a task and a quota budget. It works inside that budget, reserves enough quota to land, writes a resumable technical handoff, and stops.**

> Alpha MVP: Claude Code + OpenAI Codex.

**Verification status, honestly.** Codex has one fully proven Windows path;
Claude Code is still complete only on surfaces where its status line runs:
>
> | | Reads real quota | Enforces the floor |
> | --- | --- | --- |
> | **Claude Code** | only where the status line runs — see below | **verified** 2026-08-27: a blocked write did not run |
> | **Codex** | **verified** on codex-cli 0.150.0-alpha.8 | **verified** 2026-08-28 on Windows: `apply_patch` was denied and no file landed |
>
> Everything degrades safe rather than silently: unknown quota conserves, it is
> never read as 100%. Host enforcement remains version-specific; rerun the
> integration check when upgrading Codex rather than assuming another build has
> the same deny behavior.

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

Codex's composer works differently. The **Agent Landing** row is an
autocomplete candidate, not a persistent argument hint. Verified on Codex
`0.149.0-alpha.4.3`: pressing Space while that row is visible dismisses it
without selecting the skill. Select the row first; Codex can then apply the
configured editable starter, `Use $landing leave 75 -- <task>`. The current
`skills/list` interface exposes `shortDescription` and `defaultPrompt`, but no
persistent `argumentHint`, so Landing cannot keep the candidate row visible
after Space.

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

For example: `/landing leave 75 -- fix the checkout tests`. In Codex, select
**Agent Landing** before typing arguments; its editable starter is
`Use $landing leave 75 -- <task>`.

Invocations are parsed by `landing invoke`, not by the model, so both hosts agree
and a typo is refused instead of silently starting an unguarded task.

Users interact with it in three places:

1. **Inside the existing Claude Code or Codex conversation** — invoke `/landing` in Claude Code or `$landing` in Codex. Natural-language budget requests can still be supported, but the explicit command is the discoverable default.
2. **In the terminal statusline** — keep a small always-visible budget indicator such as `AL | W 84->70% | safe +11% | SAFE`.
3. **On demand in the same terminal** — run `landing status` for details or `landing status --compact` for the one-line view.

The local web dashboard is optional diagnostics/history only. It should never be required for normal use and should not pop up automatically.

The installer prints this once after setup:

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

To end the contract itself rather than one task, send `/landing cancel` (Codex: `$landing cancel`) as a whole message. The guard stands down, later instructions do not restart the floor, and the session is kept on disk so its burn history remains useful to the next budget.

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

### The status line is the only Claude quota source, and it does not run everywhere

Claude Code reports `rate_limits` to the status line and nowhere else, so that
one command is the entire Claude quota path. **Some Claude Code surfaces never
invoke it.** Verified on Windows on 2026-08-27: the `PreToolUse`, `PostToolUse`
and `UserPromptSubmit` hooks all ran normally for a whole session while the
configured `statusLine` command was never called once.

Where that happens the enforcement half still works — the gate blocks writes,
and it was verified doing so on the same session — but it has no quota to
enforce against. The guard reports `QUOTA UNKNOWN`, stays in `CONSERVE`, and
never fabricates headroom, so it fails safe. It cannot hold a floor.

Practically: **Landing's Claude support is only complete in the terminal CLI.**
Run `landing doctor` and read the `quota_source` line. `pending` after a session
of normal work means this is the situation you are in.

Two further limits on the same path: `rate_limits` appears only for Claude.ai
subscriptions and only after the first API response of a session, so API-key
users cannot be guarded at all; and model-scoped weekly limits visible in
`/usage` are not always projected into the status line. An unknown limit is
never treated as 100% available.

## Codex

Codex exposes ChatGPT quota through the stable app-server method `account/rateLimits/read`. Agent Landing queries this runtime RPC directly; quota math does not require a model turn.

See [`examples/codex/config.toml`](examples/codex/config.toml).

The Codex hook refreshes a stale quota snapshot before evaluating a tool call. Current Codex hook support is still evolving across CLI/Desktop builds, so v0.1 targets the documented/implemented hook schema and treats runtime hook regressions as a platform limitation rather than fabricating quota state.

## Install once for every project

Install Landing into a persistent Python environment, then let it merge the
user-level configuration for both hosts:

```bash
# From a downloaded or cloned checkout. pipx keeps the runtime independent of
# this repository, so the hooks do not break if the checkout is moved later.
pipx install .
landing install
landing doctor
```

Once published, the same environment can be created from PyPI instead of a
checkout:

```bash
pipx install agent-landing
```

The distribution is `agent-landing`; the command it installs is `landing`.

`landing install` is also the update command: run it again after upgrading the
package. The installed skill is a *rendered* copy — absolute interpreter paths
are substituted in — so a package upgrade leaves the old one in place. Skipping
this does not weaken enforcement, which lives in the hooks, but it can silently
lose plain-language triggering: a phrase like "leave me 70%" then arms nothing
while the user believes it did. `landing doctor` reports a stale copy and names
the command that fixes it.

It installs the skill and hooks at user scope, so no copying is needed inside
each repository:

| Host | Persistent skill | Persistent configuration |
| --- | --- | --- |
| Codex | `~/.agents/skills/landing/` | `~/.codex/hooks.json` |
| Claude Code | `~/.claude/skills/landing/` | `~/.claude/settings.json` |

The merge preserves unrelated settings and hooks. It refuses to overwrite invalid
JSON or an unowned skill directory with the same name. Hook commands point at the
Python environment that installed Landing, not at this repository.

Claude Code has exactly one `statusLine` slot, and it reports `rate_limits`
there and nowhere else — so that slot is Landing's only quota source. Landing
**shares** it rather than taking it: an existing status line is wrapped, so
Landing ingests quota from the payload and then runs your command with the same
payload and prints its output unchanged. `landing uninstall` puts your original
command back. A command containing a double quote cannot be re-quoted safely, so
that one case is refused instead of guessed at.

The budget itself lives in `~/.landing/`, not in the repository, because quota
is account-wide: a floor set in one checkout keeps guarding when the agent moves
to another against the same quota. Use `--state-scope project` (or
`LANDING_STATE_SCOPE=project`) for one budget per checkout; `LANDING_HOME` still
overrides both. `landing install` adopts a budget left in an older
project-scoped `.landing/` rather than appearing to forget the floor.

Codex deliberately requires one manual security confirmation after its hook
file changes. Open Codex, review the pending hooks, and trust the three Landing
entries. Use **Trust all and continue** only if every pending hook is expected;
the review screen can include unrelated plugins. Then rerun `landing doctor`.
Doctor asks the local app-server whether
all three hooks are loaded from user scope and trusted; it does not spend a
model turn. On Windows it also discovers the Codex Desktop executable when it
is not on PATH.

Install only one host when needed:

```bash
landing install --host codex
landing install --host claude
```

Remove Landing without touching unrelated user configuration:

```bash
landing uninstall
```

See [`docs/INSTALLATION.md`](docs/INSTALLATION.md) for update, conflict, trust,
and verification details. The files under `examples/` remain manual reference
configurations for development; they are not required in each project.

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
6. **The gate fails closed.** A hook that crashes is a hook the host ignores, which would leave the session unguarded while the user still believes it is protected. If the budget cannot be evaluated, work is denied and the message says how to stand the guard down. Where no budget was ever requested, an internal error blocks nothing.

## Technical documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, data flow, state machine, invariants, and provider boundary.
- [`docs/TESTING.md`](docs/TESTING.md) — unit, simulation, Claude Code, Codex, and dashboard test plan.
- [`CLAUDE.md`](CLAUDE.md) — repository-level instructions for Claude Code reviewers/testers.

## v0.1 limitations / next work

- Find out why some Claude Code surfaces never invoke the status line, and
  whether quota can be reached any other way on them. Until then Landing's
  Claude support is complete only in the terminal CLI.
- Validate on macOS and Linux. Everything verified so far was on Windows, where
  Application Control additionally blocks the `landing` console script and
  Codex's shell helper, so all commands must go through `python -m
  agent_landing.cli`.
- Add task-history/burn-rate charts and a compact cross-provider statusline mode.
- Add model-scoped Claude limits when/if the runtime exposes them to statusLine.
- Make landing-safe test commands configurable instead of using a minimal built-in allowlist.
- Add a local event log so the handoff can include commands/tests even when the model cannot spend a final turn.
- Add native marketplace/plugin distribution as an optional channel after the
  cross-host package installer is proven.

### Codex enforcement boundary

Codex enforcement was verified on Windows with codex-cli
`0.150.0-alpha.8` on 2026-08-28. A no-budget control run could create a file;
with the same write path in LAND, Codex reported that the `PreToolUse` hook
blocked `apply_patch`, and the target file did not exist afterwards. This proves
the tested host build acts on Landing's deny rather than merely displaying it.

That result is deliberately version-scoped. Other Codex builds have reported
deny-path regressions, so an upgrade is not assumed safe until the same control
and LAND pair is rerun.

## License

MIT

<!-- Documentation sync check. -->
