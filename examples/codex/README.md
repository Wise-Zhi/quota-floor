# Codex integration

Verified against **codex-cli 0.149.0-alpha.4.3** on Windows 11. Hooks are a
stable, default-on feature there (`codex features list` shows `hooks stable
true`), and the payload format matches Claude Code's closely enough that Agent
Landing's hook handlers work unchanged.

## Composer discovery

The **Agent Landing** row shown while searching in the Codex composer is an
autocomplete candidate, not a persistent argument hint. On the verified build,
pressing Space while the row is visible dismisses it without selecting the
skill. Select the candidate first; the configured editable starter is
`Use $landing leave 75 -- <task>`.

The same build's `skills/list` result exposes `shortDescription` and
`defaultPrompt` for the skill, but no persistent `argumentHint` field. Treat
this as a host UI boundary: the disappearing row says nothing about Landing's
argument parser or whether its hooks are installed, trusted, and enforced.

## Install

Copy `hooks.json` to one of these, and replace the placeholder paths with real
absolute ones:

| Location | Scope |
| --- | --- |
| `<repo>/.codex/hooks.json` | this project only (`source: project`) |
| `$CODEX_HOME/hooks.json`, normally `~/.codex/hooks.json` | every project (`source: user`) |

Only one representation per layer: a layer may have `hooks.json` **or** a
`hooks/` directory, not both, or Codex warns and prefers one.

`commandWindows` overrides `command` on Windows, so a single file can serve both
platforms. Verified: with both set, `hooks/list` on Windows reports the
`commandWindows` value as the effective command.

Use the interpreter (`python -m agent_landing.cli`), not the `landing` console
script. The script is an unsigned pip-generated executable and Windows
Application Control blocks it on some machines — and a hook that cannot start
does not block anything, so the session ends up silently unguarded.

## Hooks must be trusted, or they do not run

On first use Codex shows a **"Hooks need review"** prompt with the options
*Trust all and continue* and *Continue without trusting (hooks won't run)*.
Choosing the second leaves you unguarded while everything looks installed.

The same applies to directory trust: project-local hooks stay disabled until the
directory itself is trusted ("Do you trust the contents of this directory?").

Codex records a `trusted_hash` per hook file, so **editing `hooks.json` requires
re-trusting it**. Re-check after any change to the file.

## Verify without spending quota

`hooks/list` on the app-server reports exactly what Codex loaded, including parse
errors, and costs no model turn:

```bash
printf '%s\n' '{"method":"initialize","id":1,"params":{"clientInfo":{"name":"probe","title":"probe","version":"0.1.0"}}}' '{"method":"initialized"}' '{"method":"hooks/list","id":2,"params":{}}' | codex app-server
```

Keep the input open a second or two; closing stdin immediately makes
`app-server` drop the request. Match responses by `id` — unrelated notifications
are interleaved into the stream.

Check **`trustStatus`**, not just `enabled`. Every record carries both, and they
answer different questions:

| Field | Means |
| --- | --- |
| `"enabled": true` | the entry is switched on in the file |
| `"warnings": []` | the file parsed |
| `"trustStatus": "trusted"` | **Codex will actually run it** |

A hook can be `enabled: true` with zero warnings and still be
`"trustStatus": "untrusted"`, in which case the host never invokes it and
nothing is guarded. Print the whole record rather than the fields you expect:

```bash
... | python -c "import sys,json;[print(json.dumps(h,indent=2)) for l in sys.stdin if (m:=json.loads(l)).get('id')==2 for h in m['result']['data'][0]['hooks']]"
```

Values seen: `untrusted`, `trusted`, `modified` (edited since last trusted, needs
re-review), `managed` (org-managed, always on).

A malformed file shows up as a warning naming the exact field, for example:

```
failed to parse hooks config ...\.codex\hooks.json:
unknown field `PreToolUse`, expected `description` or `hooks`
```

So `hooks/list` answers two of the three questions: the file parsed, and Codex
will run it. It still does not prove the host acts on a `deny`; see below.

## Granting trust

There is no `hooks/trust` app-server method and no `codex hooks trust`
subcommand. Trust is granted **interactively**: start `codex` in the directory,
and the startup review screen appears —

```
Hooks need review
Hooks can run outside the sandbox after you trust them.
  Trust all and continue
  Continue without trusting (hooks won't run)
```

Until that happens `hooks/list` reports `untrusted` and the guard is inert. The
same screen returns as `modified` after any edit to `hooks.json`, because trust
is bound to the file's `currentHash`.

An `-c bypass_hook_trust=true` override exists for the app-server. It skips the
review entirely, which is a security control, not a formality — hooks run
outside the sandbox. Prefer the review screen.

## Then check enforcement

Force `LAND` with a floor just above current quota, which costs nothing:

```bash
landing invoke "leave 5h 90 -- wiring check" --provider codex
```

Feed a synthetic payload straight to the hook to confirm Agent Landing's side
before involving the model at all:

```bash
echo '{"hook_event_name":"PreToolUse","tool_name":"apply_patch","tool_input":{"file_path":"src/x.py"},"tool_use_id":"t","session_id":"s","cwd":".","model":"m","permission_mode":"default","transcript_path":null,"turn_id":"u"}' | landing hook-pre-tool --platform codex
```

Expected in `LAND`, all verified on 0.149.0-alpha.4.3:

| Tool call | Decision |
| --- | --- |
| `apply_patch` to an arbitrary file | `deny` |
| `apply_patch` to `AGENT_HANDOFF.md` | allowed |
| `shell` running `git status` | allowed |
| `shell` running `git status && python -c ...` | `deny` |
| `read_file` | allowed |

**What this does not prove:** that the Codex *host* acts on the `deny`. That
needs one real turn. Run it, then record which of these you saw:

- **(a) host-side gap** — the hook returned `deny` but the tool ran anyway.
- **(b) Agent Landing gap** — the hook did not return `deny` in the first place.

The command above distinguishes them: if it prints `deny` and Codex still runs
the tool, it is (a). Record the exact `codex --version` either way, because hook
enforcement has varied between builds.

## If `codex` is not on PATH

`landing start` reads live quota by spawning `codex app-server`. When the binary
cannot be found it now falls back to the last stored snapshot, and if there is
none it refuses with a readable message rather than a traceback. Point it at the
binary directly:

```bash
landing invoke "leave 70 -- refactor auth" --provider codex --executable /path/to/codex
```

## Quota polarity

Codex reports `usedPercent` (consumed), Claude Code reports `used_percentage`.
Agent Landing decides polarity from the field name only, never from the value —
`30` is identical whether it means 30% spent or 30% left. A payload whose
polarity cannot be established is recorded as unreadable, which forces
`CONSERVE`, rather than being guessed at.

`tests/test_codex_rate_limits_contract.py` pins a real payload. Re-capture it
when the Codex version moves; `docs/TESTING.md` has the procedure.
