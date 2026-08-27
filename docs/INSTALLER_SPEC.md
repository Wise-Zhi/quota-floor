# Persistent user-level installation

## Objective

Provide one reproducible installation command for the public Landing package so
Claude Code and Codex discover the skill and quota hooks in every repository.
Re-running the command updates Landing-owned files. Uninstall removes only those
files and entries.

## Technology and commands

- Python 3.10+ and the existing `landing` console entry point.
- `landing install [--host all|codex|claude]`
- `landing doctor [--host all|codex|claude]`
- `landing uninstall [--host all|codex|claude]`

`all` is the default. Commands accept an optional `--home` for deterministic
tests; normal use resolves the current user's home directory.

## Installed layout

| Host | Skill | Configuration |
| --- | --- | --- |
| Codex | `~/.agents/skills/landing/` | merge into `~/.codex/hooks.json` |
| Claude Code | `~/.claude/skills/landing/` | merge into `~/.claude/settings.json` |

Skill sources are package data under `agent_landing/assets`, so a wheel or a
GitHub install does not depend on the checkout that built it. Hook commands use
the Python executable that owns the installed Landing package and invoke
`python -m agent_landing.cli`; they contain no repository path.

## Ownership and merge rules

- Preserve all unrelated keys, hooks, and skill directories.
- Recognize Landing hook commands by the module invocation plus the specific
  `hook-*` command and platform; replace those entries on update instead of
  appending duplicates.
- Write a Landing ownership marker inside copied skill directories. Uninstall
  removes a skill directory only when that marker is present.
- Claude Code has one `statusLine` slot and reports `rate_limits` only there, so
  Landing shares it rather than claiming it: an existing non-Landing command is
  wrapped via `claude-statusline --wrap <command>`, and uninstall restores it.
  The wrapped command lives in Landing's own status line command, not in
  separate state, so it cannot go stale against what it describes. A command
  containing a double quote cannot be re-quoted safely; report that conflict and
  leave Claude configuration unchanged.
- Local state is user-scoped (`~/.landing/`) because quota is account-wide.
  `install` copies a budget left in the current checkout's `.landing/` up to the
  user-level root unless one is already in force there, and never deletes the
  original.
- Use atomic file replacement for JSON writes. Invalid existing JSON is a hard
  error and is never overwritten.
- Installing Codex hooks does not manufacture trust. The user reviews them in
  Codex; `doctor` reports whether the app-server says they are trusted.

## Output contract

Every command emits JSON and exits non-zero when a requested host failed.
Installation reports changed paths and any required next action. Doctor reports
each check as `ok`, `warning`, or `error`. Uninstall reports only entries it
actually removed.

## Testing strategy

- Unit tests use temporary homes and synthetic existing configuration.
- Cover clean install, preservation of unrelated data, idempotent update,
  status-line conflict, invalid JSON, and conservative uninstall.
- Assert packaged skill assets match the repository's host-facing skill files.
- CLI tests exercise all three commands without touching the real user profile.
- Run the complete existing suite after focused tests.
- Final smoke test uses an isolated home; live installation is verified through
  Codex `skills/list` and `hooks/list` without spending a model turn.

## Boundaries

- Do not install per repository as the normal public workflow.
- Do not alter or trust unrelated hooks.
- Do not bypass Codex's hook review security boundary.
- Do not auto-edit shell startup files or install Python itself.
- Repository-local configurations remain development examples, not a public
  user's required setup.

## Success criteria

1. One install command enables both hosts for all repositories owned by the
   current user.
2. A second install produces no duplicate hooks and safely refreshes assets.
3. Existing unrelated user settings survive byte-for-byte at the data level.
4. Uninstall removes only Landing-owned entries and directories.
5. Doctor identifies missing, stale, untrusted, or undiscoverable installation.
6. Wheels contain every installed skill asset.
7. Full test suite passes on Windows and path rendering is portable to POSIX.

## Open questions deferred from v0.1

- A native Claude plugin/marketplace can become an additional distribution
  channel later; it is not required for the cross-host installer.
- Codex may add a first-party trust/install API later. Until then review remains
  an explicit UI action.

