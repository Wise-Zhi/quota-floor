# Changelog

All notable changes to Quota Floor are recorded here.

## [0.1.0a2] - 2026-10-15

### Fixed

- `landing doctor` no longer reports `hook_runtime` as an error on macOS and
  Linux. A virtualenv's `python` there is a symlink; the doctor compared only
  the resolved path, so every POSIX install looked stale.

## [0.1.0a1] - 2026-10-15

First release on PyPI, as `quota-floor`. It contains everything below.

### Changed

- The handoff is no longer a file in the working folder. In LAND the agent's
  final reply is the handoff; a new `Stop` hook saves it to
  `~/.landing/handoffs/`. LAND now denies every write, with no handoff exception.
  The `PostToolUse` hook is retired and removed on reinstall. Codex needs its
  hooks re-trusted after `landing install`.
- `/landing` reads plain phrasing (`leave me 70%`, `spend at most 15%`) and
  Chinese keywords (留/保留/剩, 花/花费/用); autocomplete shows
  `leave|spend <percent> [task]`.
- Under a budget, the agent does the most valuable part of the task first.
- A budget covers one instruction and ends by itself when that turn ends; the
  agent no longer has to run `landing complete`. A message added mid-task never
  ends it, and after an interrupt it ends with the next turn. `/landing cancel`
  remains as the way out when something is stuck.

### Fixed

- An agent can now mark its task complete. The gate used to deny the
  `landing complete` it was told to run, and the command had no conversation to
  act on, so a task that finished early kept its budget armed until
  `/landing cancel`. The `PreToolUse` hook now completes that conversation's
  budget itself and freezes the rest of the turn. Only the bare command counts,
  and only before the boundary; anything chained to it is still denied.
- `landing status` (and `--compact`) in a terminal shows every live budget,
  including those opened from a chat, instead of only the default one.

### Removed

- `landing start --handoff`.

### Documentation

- The README is rewritten for first-time readers. Internals moved to
  `docs/HOW_IT_WORKS.md` and the live-host record to `docs/VERIFICATION.md`.
  New `docs/COMPARISON.md` covers existing quota tools, and `docs/DEMO.md` is a
  zero-quota demo script.
- GitHub issue templates ask for the host version, and for whether the host
  ignored a decision or Landing made the wrong one.
- More PyPI classifiers and keywords.

### Known issues

- The final-reply handoff saved by the `Stop` hook is not yet verified on a live host.

## [0.1.0-alpha] - 2026-08-29 (baseline, not published)

First public alpha candidate.

### Added

- Quota-floor state machine with dynamic landing reserve and resumable handoffs.
- Codex and Claude Code hooks, persistent installation, diagnostics, and safe
  uninstall behavior.
- Codex app-server quota snapshots and Claude status-line quota ingestion.
- Runway telemetry kept advisory and isolated from enforcement decisions.
- Regression coverage for quota parsing, persistence, hook gating, emergency
  fallback, and early completion.

### Safety

- Treats every requested floor as a limit, never a spending target.
- Conserves when quota is missing or unreadable instead of assuming 100%.
- Blocks mutating tools during landing while retaining read-only inspection.
- Recognizes Codex's freeform `apply_patch` hook payload while allowing only a
  patch whose every operation writes the exact reserved handoff path.
- Installs no Codex skill; hooks enforce the policy without adding model-facing
  invocation instructions.

### Known limitations

- Host hook enforcement is version-specific and must be reverified after host
  upgrades.
- Some Claude Code surfaces do not invoke the configured status line, leaving
  quota unknown and the guard conservatively restricted.
- Live integration evidence currently covers Windows; other platforms still
  require host-level verification.
