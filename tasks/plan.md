# Persistent installer implementation plan

## Phase 1: Contract

- Freeze paths, ownership rules, conflict behavior, output, and acceptance tests.
- Record the untouched user-owned working-tree changes.

## Phase 2: Installation core

- Add failing tests for merge, idempotence, conflict, invalid JSON, and uninstall.
- Implement host-independent filesystem and JSON helpers.
- Implement Codex and Claude configuration generation.

## Phase 3: Distribution and CLI

- Package both host skill definitions as wheel data.
- Add `install`, `doctor`, and `uninstall` subcommands.
- Add CLI and package-integrity tests.

## Phase 4: Documentation and proof

- Replace manual-first setup instructions with the persistent workflow.
- Test an isolated installation and build the wheel.
- Verify the real user-level Codex discovery/trust state without a model turn.
- Review the diff and commit only installer-owned changes.
