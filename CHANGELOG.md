# Changelog

All notable changes to Agent Landing are recorded here.

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
