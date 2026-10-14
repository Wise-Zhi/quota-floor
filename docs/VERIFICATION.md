# Verification record

What has been checked against real Claude Code and Codex hosts, and what has
not. Every claim is scoped to the host version and date it was observed on.
Tests in `tests/` cover the policy; this page covers whether the hosts act on it.

## Summary

| | Reads real quota | Enforces the floor |
| --- | --- | --- |
| **Claude Code** | only where the status line runs (see below) | **verified** 2026-08-27 on Windows: a blocked write did not run |
| **Codex** | **verified** on codex-cli 0.150.0-alpha.12.2 | **verified** 2026-08-29 on Windows: implementation patches were denied in LAND |

**Not yet verified live:** the current handoff path. In LAND the agent's final
reply is the handoff and a `Stop` hook saves it to `~/.landing/handoffs/`; LAND
denies every write. This is covered by unit tests, but
no live LAND run has exercised it on either host. The live runs below used the
earlier design, in which the handoff was a file the agent was allowed to write.

Everything verified so far was on Windows. macOS and Linux run the test suite in
CI but have no live host evidence.

## Claude Code

Claude Code's status-line payload exposes the base `five_hour` and `seven_day` usage windows. `landing install` configures `landing claude-statusline` so the runtime quota is persisted locally; the hooks (`PreToolUse` gate, `UserPromptSubmit`, `Stop`) enforce against it.

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

Codex exposes ChatGPT quota through the app-server method `account/rateLimits/read`. Agent Landing queries this runtime RPC directly; quota math does not require a model turn. The Codex hook refreshes a stale quota snapshot before evaluating a tool call.

### Enforcement boundary

Codex enforcement was verified on Windows with codex-cli
`0.150.0-alpha.12.2` on 2026-08-29. A no-budget control run could create a file;
with the same write path in LAND, Codex reported that the `PreToolUse` hook
blocked `apply_patch`, and the target file did not exist afterwards. A second
LAND run allowed an `apply_patch` that touched only the reserved
`AGENT_HANDOFF.md`; the file landed and `PostToolUse` stopped the session. This
proves the tested host build acts on both sides of Landing's gate rather than
merely displaying its decision.

The handoff half of that run tested a design that has since been replaced: the
handoff is now the final reply, saved by a `Stop` hook, and LAND denies every
write. Codex 0.162 exposes `Stop` with `last_assistant_message`, but the new
path still needs a live LAND run.

That result is deliberately version-scoped. Other Codex builds have reported
deny-path regressions, so an upgrade is not assumed safe until the same control
and LAND pair is rerun.

### Codex gets no skill, on purpose

The `UserPromptSubmit` hook parses the invocation and opens the budget before the
model is given the turn, so nothing about a budget needs the model's cooperation.
Landing therefore installs **no Codex skill**. An invocation is simply text in the
message, which the hook reads.

That is not a limitation, it is the fix. Codex names the skills it has loaded at
the start of a turn, so merely *being* a skill made every budgeted task open with
`I'm using the requested Landing workflow …` instead of the work. Measured on
2026-08-28, same task and prompt run twice with only the skill's presence
differing: with the skill, Landing led the first sentence; without it, Landing did
not appear in the transcript at all, while the budget still opened and the task
still completed. No wording inside a skill can stop a model from mentioning that
the skill exists.

The cost is discoverability: Codex cannot autocomplete a skill that is not there.
Claude Code keeps its skill, because its slash command is how `/landing` is
offered in the first place.

## Platform notes

On the Windows machine used for verification, Application Control at one point
blocked the `landing` console script and Codex's shell helper. Hook commands are
therefore always written as `<python> -m agent_landing.cli <subcommand>` against
the interpreter that installed Landing, never as the bare `landing` script: a
hook that cannot start blocks nothing, and the host runs the tool anyway.

## Reproducing

The integration procedures are in [TESTING.md](TESTING.md#claude-code-integration-test)
and [TESTING.md](TESTING.md#codex-integration-test). When reporting a result,
record the host name and version, and say whether a failure is the host ignoring
a hook decision or Landing making the wrong decision.
