# Persistent installation

Landing is installed once per operating-system user. It is not copied into each
project. The Python package owns the executable runtime; `landing install` owns
only the Landing skill directories and Landing hook entries.

## Install

From a cloned or downloaded checkout:

```bash
pipx install .
landing install
```

`pipx` is recommended because it keeps a stable Python environment after the
checkout is moved or deleted. A normal virtual environment also works, but that
environment must remain at the same path for the hooks to start.

The default installs both hosts. Use `--host codex` or `--host claude` to limit
the operation. Re-running the same command safely updates Landing-owned files
and replaces old Landing hook entries instead of adding duplicates.

## What is installed

### Codex

- Skill: `~/.agents/skills/landing/`
- Hooks: merged into `~/.codex/hooks.json`
- Events: `PreToolUse`, `PostToolUse`, and `UserPromptSubmit`

Codex hook commands invoke the Python interpreter that owns Landing. The
installer also locates the Codex Desktop binary on Windows when `codex` is not
on PATH, so the pre-tool hook can refresh quota through app-server.

### Claude Code

- Skill: `~/.claude/skills/landing/`
- Hooks and status line: merged into `~/.claude/settings.json`
- Events: `PreToolUse`, `PostToolUse`, and `UserPromptSubmit`

Claude Code supports only one `statusLine`, and it reports `rate_limits` there
and nowhere else — so that single slot is Landing's only quota source on Claude.
Landing therefore **shares** it instead of claiming it. An existing status line
is wrapped:

```json
{ "statusLine": { "type": "command",
  "command": "/path/to/python -m agent_landing.cli claude-statusline --wrap 'my-status --fancy'" } }
```

Landing reads quota from the payload, then runs your command with that same
payload and prints its output unchanged. Your status bar looks exactly as it did.
If your command fails or times out, Landing prints its own quota line rather than
leaving the bar empty, and says why on stderr.

`landing uninstall` restores your original command. Re-running `landing install`
keeps wrapping the same command rather than wrapping its own wrapper.

One case is still refused: a status line command containing a double quote
cannot be re-quoted safely, and a mangled status line is a mangled quota source.
Move such a command into a script and rerun; the failed host is left unchanged.

## Where the budget lives

State lives in `~/.landing/` by default, not in the repository. Quota is
account-wide, so the budget is too — a floor set in one checkout keeps guarding
when the agent moves to another and burns the same quota, and burn history
accumulates across projects instead of restarting the landing reserve in each
new clone.

| Setting | Effect |
| --- | --- |
| default | `~/.landing/` — one budget for the account |
| `--state-scope project` or `LANDING_STATE_SCOPE=project` | `<cwd>/.landing/` — one budget per checkout |
| `LANDING_HOME` / `AGENT_LANDING_HOME` | an explicit directory; wins over both |

Landing used to be project-scoped. `landing install` copies a budget left in the
current checkout's `.landing/` up to the user-level root, unless one is already
in force there. The original is left in place rather than deleted.

## Codex trust review

Installing a hook and trusting it are separate steps. Landing never edits
Codex's trust records.

1. Open Codex after installing or updating.
2. At **Hooks need review**, review and trust the three Landing commands. Use
   **Trust all and continue** only if every pending hook is expected, because
   unrelated plugin hooks can appear in the same review.
3. Run `landing doctor --host codex`.

The doctor calls `hooks/list` and `skills/list` on the local app-server. A ready
installation reports all three hooks as loaded from `user` scope, enabled, and
trusted, and reports the `landing` skill enabled at user scope. This check does
not invoke a model.

## Verify both hosts

```bash
landing doctor
```

The command returns non-zero if files are missing or stale, a hook is missing,
Codex did not discover the user skill, or Codex hooks are not trusted. Use
`--no-live` only when Codex is unavailable and a filesystem-only check is
intentional.

### `quota_source`

Every other check proves the commands are *wired*. `quota_source` reports
whether a quota reading has ever actually arrived, because wired and working are
not the same thing — a Claude Code surface that renders no status line still
runs the hooks, so the install looks perfect while the guard has no quota at all.

| Status | Meaning |
| --- | --- |
| `ok` | a reading for this host arrived within the last 24 hours |
| `stale` | the newest reading is older than 24 hours; the path may have stopped delivering |
| `pending` | no reading has ever been recorded for this host |

`pending` is expected immediately after installing: Claude publishes quota only
through the status line, and only after the first API response of a session. If
it is still `pending` after working normally for a while, the surface is not
invoking the status line and the Claude guard is running blind — it will
conserve rather than fabricate headroom, but it cannot enforce the floor.

None of these three ever fail the command. A missing quota reading is a fact
about the host, not a broken installation.

After doctor passes, open any other repository. Claude Code should offer
`/landing`; Codex should show **Agent Landing** when selecting `$landing`. Codex
does not keep its autocomplete candidate visible after an unselected Space;
that composer behavior is independent of installation and hook trust.

## Update

Upgrade or reinstall the Python package in its persistent environment, then run:

```bash
landing install
landing doctor
```

Codex hashes hook files, so a changed file may require trust review again.

## Uninstall

```bash
landing uninstall
```

Uninstall removes a skill directory only when it contains Landing's ownership
marker. In JSON configuration it removes only hook commands identifiable as
Landing and the Landing status line. Other hooks, permissions, and settings are
preserved.

## Manual reference configuration

The `examples/claude` and `examples/codex` directories document the host wire
format and remain useful for development or forensic verification. Public users
should prefer the installer because it renders absolute runtime paths and merges
configuration without requiring a per-project copy.
