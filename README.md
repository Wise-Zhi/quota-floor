# Agent Landing

**Tell Claude Code or Codex how much of your subscription quota to leave untouched. When quota gets close to that floor, Landing blocks further edits, and the agent writes a handoff and stops.**

```text
/landing leave 70 fix the tests
```

That gives the agent one task and a floor: at least 70% of your quota must remain
when it stops. Landing reads your real quota outside the model, keeps a small
reserve so the agent has room to write the handoff, and blocks every write once
it hits that reserve. If the task finishes at 91%, it stops at 91%. **The floor is a limit, never a spending target.**

> **Alpha.** Windows is the only platform verified on real hosts, and only on the host builds listed under
> [Verification status](#verification-status).

## Install

```bash
pipx install agent-landing
landing install
landing doctor
```

Until the PyPI release exists, install from GitHub instead of the first line:
`pipx install git+https://github.com/zhixun123/landing`.
`landing install` configures Claude Code and Codex for your user account, not
for one project. It keeps your existing settings, and `landing uninstall`
reverses it. Details: [docs/INSTALLATION.md](docs/INSTALLATION.md).

## Why

A coding agent keeps working until the task is done, even when the rest of your
week's quota goes with it. The guards that exist today don't fit a subscription:

- **Claude Code's `--max-budget-usd`** caps API dollars. A Claude.ai
  subscription is measured in percent of a five-hour and a weekly window, not
  in dollars.
- **Exhaustion blockers** stop work only when a window reaches 0%. By then
  there is no quota left to write down what was done.
- **Usage viewers** show the numbers and enforce nothing.

Landing lets you choose the floor, reserves quota so the agent can stop
cleanly above it, and never treats unknown quota as full. Full comparison: [docs/COMPARISON.md](docs/COMPARISON.md).

## What happens

```text
you:      /landing leave 70 fix the tests
landing:  budget opened -- floor 70% on every window, reserve 4%
agent:    ...works normally (NORMAL), then carefully (CONSERVE)...
landing:  quota reaches 74% (70% floor + 4% reserve) -> LAND
agent:    tries to edit src/app.py
landing:  denied: "Quota budget boundary reached. Editing is blocked;
          write the technical handoff as your final reply, then stop."
agent:    final reply = handoff (done, changed, tested, next steps, resume prompt)
landing:  saved to ~/.landing/handoffs/<time>-fix-the-tests.md
```

Writes are denied in LAND. Read-only inspection is still allowed, so the
handoff is accurate. Nothing is written into your project folder.

A scripted, zero-quota walkthrough of this flow is in [docs/DEMO.md](docs/DEMO.md).

## Usage

| Host | Type this |
| --- | --- |
| Claude Code | `/landing leave 70 fix the tests` |
| Codex | `$landing leave 70 fix the tests` |
| Terminal | `landing status`, `landing status --compact` |

- `leave 70` stops while at least 70% **remains**. `spend 15` spends at most
  15 percentage points. One of the two keywords is required. A bare number is
  refused, so a dropped keyword can never be read as the opposite request.
- The task is optional. `/landing spend 20` on its own puts a floor under the
  work already in progress.
- `leave` applies to **every** quota window your plan reports (five-hour and
  weekly) and triggers on whichever window reaches it first. `spend` is
  measured on the five-hour window. Add `weekly` or `5h` to choose the window
  yourself, e.g. `/landing leave weekly 70 fix the tests`.
- A budget lasts one task: it ends by itself when the agent's turn ends.
- Plain phrasing works too: `/landing leave me 70% quota`,
  `/landing spend at most 15% on this`, `/landing 留 70 修测试`. A qualifier
  that contradicts the keyword (`leave at most 30`) is refused, not guessed.
- Settings may come before or after the task: `fix the tests leave 70` is the
  same request.
- `/landing cancel` (Codex: `$landing cancel`), sent as the whole message,
  ends the budget. The agent cannot cancel its own budget: the gate denies it.

Invocations are parsed by the `UserPromptSubmit` hook, in code, before the
model receives the message. A typo is refused, so it cannot quietly start a
task with no budget.

## How it works

```text
current remaining quota - your floor - landing reserve = room to work
```

| State | Behavior |
| --- | --- |
| **NORMAL** | Work normally. |
| **CONSERVE** | Close to the boundary: finish the current objective, start nothing new. |
| **LAND** | Every write is denied. The agent's final reply is the handoff, saved by a `Stop` hook. |
| **EMERGENCY** | No quota for another model turn: a deterministic handoff is written locally from task metadata and Git state. |
| **STOPPED** | The budget is over until a new one is started. |

- **Enforcement uses only real quota snapshots.** On Claude Code they come
  from the status line. On Codex they come from `account/rateLimits/read`.
  No model turn is spent checking quota.
- **Unknown quota is never read as 100%.** Missing, stale, or unreadable quota
  means `CONSERVE` with `QUOTA UNKNOWN`. A hook that fails while a budget is
  active denies the write.
- **The reserve is measured, not guessed.** It is the p90 of observed burn per
  tool call, priced for the landing sequence and clamped to 3-15%.
- **Runway is advisory.** `landing status` and the dashboard show how long the
  remaining room lasts at the observed burn rate. That estimate never changes
  what is enforced.

More: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) (states, windows, reserve,
runway, handoff contract) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
(components and invariants).

## Verification status

| | Reads real quota | Enforces the floor |
| --- | --- | --- |
| **Claude Code** | only where the status line runs (terminal CLI) | **verified** 2026-08-27 on Windows: a blocked write did not run |
| **Codex** | **verified** on codex-cli 0.150.0-alpha.12.2 | **verified** 2026-08-29 on Windows: implementation patches were denied in LAND |

- 2026-10-15, current design: on Codex (ChatGPT desktop, cli 0.162.0-alpha.17.2)
  LAND denied writes and the `Stop` hook saved the final-reply handoff. On
  both Codex and Claude Code (terminal CLI 2.1.211), a `spend 15` budget
  opened on its own conversation, the task finished early, and the budget
  ended with the turn. Claude Code has not yet been driven into LAND under
  this design.
- Enforcement depends on how each host version handles hook denials. After
  upgrading Codex, rerun the integration check before trusting it.
- Some Claude Code surfaces never run the status line. There, Landing cannot
  read quota, so it reports `QUOTA UNKNOWN` and stays in CONSERVE. That is
  safe, but it cannot hold a floor. Run `landing doctor` and check
  `quota_source`.

The full record, with the evidence for each claim, is in
[docs/VERIFICATION.md](docs/VERIFICATION.md).

## Known limitations

- Verified on Windows only. macOS and Linux run the full test suite in CI but
  have not been checked against real hosts.
- Claude Code: subscription accounts only (API-key sessions expose no quota),
  and only surfaces that run the status line. Model-scoped weekly limits shown
  in `/usage` may not reach the status line. Claude Code reports quota only
  after its first reply, so in a fresh conversation send any message before
  `/landing`.
- Codex has no `$landing` autocomplete, because Landing intentionally installs no Codex
  skill ([why](docs/VERIFICATION.md#codex-gets-no-skill-on-purpose)).

## Documentation

- [docs/INSTALLATION.md](docs/INSTALLATION.md): install, update, trust review, uninstall
- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md): states, windows, reserve, runway, handoff
- [docs/COMPARISON.md](docs/COMPARISON.md): Landing compared with existing tools
- [docs/VERIFICATION.md](docs/VERIFICATION.md): what has been verified on real hosts
- [docs/DEMO.md](docs/DEMO.md): demo recording script
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, state machine, invariants
- [docs/TESTING.md](docs/TESTING.md): test plan and integration checks
- [CHANGELOG.md](CHANGELOG.md)

## Development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[test]"
pytest -q
landing simulate --start 100 --leave 70 --burn 0.5,0.7,0.6 --step 2
```

Bug reports are most useful with the host name and version, `landing doctor`
output, and whether the problem is the host ignoring a hook decision or Landing
making the wrong decision.

## License

[MIT](LICENSE)
