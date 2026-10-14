# Changelog

All notable changes to Agent Landing are recorded here.

## [Unreleased]

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

### Removed

- `landing start --handoff`.

## [0.1.0a1] - 2026-08-29

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
