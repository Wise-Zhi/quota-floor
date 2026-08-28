from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from .providers import find_codex_executable
from .state import resolve_state_root


HOSTS = ("codex", "claude")
EVENT_COMMANDS = {
    "PreToolUse": "hook-pre-tool",
    "PostToolUse": "hook-post-tool",
    "UserPromptSubmit": "hook-user-prompt",
}
MARKER = ".landing-install.json"

#: Hosts where Landing ships a skill. Codex is deliberately absent.
#:
#: Codex announces the skills it has loaded at the start of a turn, so merely
#: *being* a skill produced "I'm using the requested Landing workflow" before any
#: work began. Measured on 2026-08-28 with the same task and prompt run twice,
#: the only difference being whether the skill was installed: with it, Landing
#: led the sentence; without it, Landing was absent from the transcript entirely
#: while the budget still opened and the task still completed.
#:
#: No wording inside a skill can stop a model from mentioning that the skill
#: exists. The `UserPromptSubmit` hook opens the budget either way, so on Codex
#: the skill bought nothing but the preamble it was supposed to prevent.
SKILL_HOSTS = ("claude",)

#: A quota reading older than this is no longer evidence that the path still works.
QUOTA_SOURCE_STALE_SECONDS = 24 * 3600


class InstallationConflict(RuntimeError):
    """Installing would overwrite configuration Landing does not own."""


def _paths(host: str, home: Path) -> tuple[Path, Path]:
    if host == "codex":
        return home / ".codex" / "hooks.json", home / ".agents" / "skills" / "landing"
    if host == "claude":
        return home / ".claude" / "settings.json", home / ".claude" / "skills" / "landing"
    raise ValueError(f"Unsupported host: {host}")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallationConflict(f"{path} is not valid JSON; it was not overwritten: {error}") from error
    if not isinstance(value, dict):
        raise InstallationConflict(f"{path} must contain a JSON object; it was not overwritten")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _quote_command(parts: list[str], os_name: str) -> str:
    return subprocess.list2cmdline(parts) if os_name == "nt" else shlex.join(parts)


def _landing_command(command: Any, *, platform: str | None = None, action: str | None = None) -> bool:
    if not isinstance(command, str):
        return False
    is_landing = (
        "agent_landing.cli" in command
        or command.strip().startswith("landing ")
        or command.strip() == "landing"
    )
    if not is_landing:
        return False
    if platform is not None and f"--platform {platform}" not in command:
        return False
    return action is None or action in command


def _remove_landing_groups(groups: Any, *, platform: str, action: str) -> list[Any]:
    if not isinstance(groups, list):
        return []
    result: list[Any] = []
    for original_group in groups:
        if not isinstance(original_group, dict):
            result.append(original_group)
            continue
        group = dict(original_group)
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            result.append(group)
            continue
        kept = [
            hook
            for hook in hooks
            if not (
                isinstance(hook, dict)
                and (
                    _landing_command(hook.get("command"), platform=platform, action=action)
                    or _landing_command(hook.get("commandWindows"), platform=platform, action=action)
                )
            )
        ]
        if kept:
            group["hooks"] = kept
            result.append(group)
    return result


def _hook_group(
    host: str,
    event: str,
    python_executable: str,
    os_name: str,
) -> dict[str, Any]:
    action = EVENT_COMMANDS[event]
    parts = [python_executable, "-m", "agent_landing.cli", action, "--platform", host]
    # Deliberately no `--executable`: Codex binds hook trust to the command
    # string, and the Windows codex binary lives in a directory whose name
    # changes on every update. Baking the path in meant each update invalidated
    # the trust and quietly reverted the one hook that blocks to untrusted.
    # `resolve_codex_executable()` finds it at call time instead.
    command = _quote_command(parts, os_name)
    hook: dict[str, Any] = {"type": "command", "command": command}
    if host == "codex":
        hook["commandWindows"] = subprocess.list2cmdline(parts)
        hook["timeout"] = 20
    group: dict[str, Any] = {"hooks": [hook]}
    if host == "codex" and event != "UserPromptSubmit":
        group["matcher"] = "*"
    return group


#: Claude Code exposes `rate_limits` to the status line and nowhere else, so the
#: single statusLine slot is Landing's only quota source. Refusing to share it
#: locked out exactly the users who already run one -- the same people who care
#: about a quota budget -- so Landing wraps the existing command instead: it
#: ingests quota from the payload, then runs the original with the same payload
#: and forwards its output verbatim.
WRAP_FLAG = "--wrap"


def _status_line(python_executable: str, os_name: str, wrapped: str | None = None) -> dict[str, str]:
    parts = [python_executable, "-m", "agent_landing.cli", "claude-statusline"]
    if wrapped:
        parts += [WRAP_FLAG, wrapped]
    return {"type": "command", "command": _quote_command(parts, os_name)}


def _wrapped_status_line(command: Any) -> str | None:
    """Recover the command Landing is wrapping, so uninstall can restore it.

    The wrapped command lives in the Landing status line itself rather than in
    separate state: a config file that describes itself cannot go stale against
    the thing it describes, and uninstall works even from a fresh checkout.
    """
    if not isinstance(command, str):
        return None
    marker = f" {WRAP_FLAG} "
    at = command.find(marker)
    if at < 0:
        return None
    # `--wrap` is always written last, so the rest of the line is its value. It
    # has to be unquoted the way it was quoted, which is a property of the
    # machine that installed it -- not of the machine reading it now.
    tail = command[at + len(marker):].strip()
    if not tail:
        return None
    if tail.startswith("'"):
        try:
            parts = shlex.split(tail, posix=True)
        except ValueError:
            return None
        return parts[0] if parts else None
    if tail.startswith('"') and tail.endswith('"') and len(tail) > 1:
        # list2cmdline quoting. Landing refuses to wrap a command containing a
        # double quote, so there is never an escape sequence to undo here.
        return tail[1:-1]
    return tail


def _merged_config(
    host: str,
    config: dict[str, Any],
    python_executable: str,
    os_name: str,
) -> dict[str, Any]:
    merged = dict(config)
    wrapped: str | None = None
    if host == "claude" and "statusLine" in merged:
        existing = merged["statusLine"]
        command = existing.get("command") if isinstance(existing, dict) else None
        if _landing_command(command, action="claude-statusline"):
            # Re-installing over ourselves: keep whatever we were already wrapping.
            wrapped = _wrapped_status_line(command)
        elif isinstance(command, str) and command.strip():
            if '"' in command:
                # Re-quoting a command that already carries double quotes is
                # where a silent corruption would come from, and a mangled
                # status line is a mangled quota source. Refuse and say so.
                raise InstallationConflict(
                    "Claude Code's existing statusLine command contains a double quote, which "
                    "Landing cannot safely wrap. Simplify it (for example move it into a "
                    "script), or remove it, then rerun `landing install`."
                )
            wrapped = command.strip()
        elif command is not None:
            raise InstallationConflict(
                "Claude Code already has a statusLine Landing does not understand; "
                "it was not overwritten"
            )

    hooks_value = merged.get("hooks", {})
    if not isinstance(hooks_value, dict):
        raise InstallationConflict("The existing hooks value must be a JSON object")
    hooks = dict(hooks_value)
    for event, action in EVENT_COMMANDS.items():
        groups = _remove_landing_groups(hooks.get(event), platform=host, action=action)
        groups.append(_hook_group(host, event, python_executable, os_name))
        hooks[event] = groups
    merged["hooks"] = hooks
    if host == "claude":
        merged["statusLine"] = _status_line(python_executable, os_name, wrapped)
    return merged


def _asset_root(host: str):
    return resources.files("agent_landing").joinpath("assets", host, "landing")


def _skill_runtime_prefix(host: str, python_executable: str, os_name: str) -> str:
    if os_name == "nt":
        normalized_executable = python_executable.replace("\\", "/")
        quoted = f'"{normalized_executable}"'
    else:
        quoted = _quote_command([python_executable], os_name)
    if host == "codex" and os_name == "nt":
        quoted = f"& {quoted}"
    return f"{quoted} -m agent_landing.cli"


def _render_skill_asset(
    content: bytes, *, host: str, python_executable: str, os_name: str
) -> bytes:
    text = content.decode("utf-8")
    if host == "codex" and os_name == "nt":
        # Newline-agnostic on purpose. A literal "\n" match fails silently on a
        # CRLF asset, shipping a skill whose fence was never converted with
        # nothing to signal it -- and packaged assets are not line-ending
        # normalized the way tracked files are.
        text = re.sub(r"```bash(\r?\n)(?=landing invoke)", r"```powershell\1", text)
    prefix = _skill_runtime_prefix(host, python_executable, os_name)
    pattern = re.compile(r"(?<![/\$])\blanding (?=(?:invoke|start|complete|cancel|\*))")

    # Substitute only inside fenced code blocks. The prefix exists so the command
    # the model *runs* is correct; splicing an absolute interpreter path into the
    # middle of a sentence reads terribly, and it applied unevenly -- a prose
    # mention wrapped onto the next line was skipped, so one clause named the
    # full path and the next said plain `landing`, in the same sentence.
    rendered: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            rendered.append(line)
            continue
        rendered.append(pattern.sub(prefix + " ", line) if in_fence else line)
    return "".join(rendered).encode("utf-8")


def _copy_assets(
    source: Any,
    destination: Path,
    *,
    host: str,
    python_executable: str,
    os_name: str,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_assets(
                child,
                target,
                host=host,
                python_executable=python_executable,
                os_name=os_name,
            )
        else:
            content = child.read_bytes()
            if child.name == "SKILL.md":
                content = _render_skill_asset(
                    content,
                    host=host,
                    python_executable=python_executable,
                    os_name=os_name,
                )
            target.write_bytes(content)


def _skill_assets_match(
    source: Any,
    destination: Path,
    *,
    allow_marker: bool = True,
    host: str | None = None,
    python_executable: str | None = None,
    os_name: str | None = None,
) -> bool:
    if not destination.is_dir():
        return False
    expected_names = {child.name for child in source.iterdir()}
    actual_names = {
        child.name
        for child in destination.iterdir()
        if not (allow_marker and child.name == MARKER)
    }
    if actual_names != expected_names:
        return False
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            if not target.is_dir() or not _skill_assets_match(
                child,
                target,
                allow_marker=False,
                host=host,
                python_executable=python_executable,
                os_name=os_name,
            ):
                return False
        else:
            expected = child.read_bytes()
            if child.name == "SKILL.md" and host and python_executable and os_name:
                expected = _render_skill_asset(
                    expected,
                    host=host,
                    python_executable=python_executable,
                    os_name=os_name,
                )
            if not target.is_file() or target.read_bytes() != expected:
                return False
    return True


def _is_managed_skill(host: str, skill_path: Path) -> bool:
    marker_path = skill_path / MARKER
    if not marker_path.is_file():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return marker == {"owner": "agent-landing", "host": host, "format": 1}


def _remove_managed_skill(host: str, skill_path: Path) -> bool:
    """Delete a Landing-owned skill directory. Never touches an unowned one."""
    if not _is_managed_skill(host, skill_path):
        return False
    shutil.rmtree(skill_path)
    return True


def _install_skill(
    host: str, skill_path: Path, *, python_executable: str, os_name: str
) -> None:
    source = _asset_root(host)
    if (
        skill_path.exists()
        and not _is_managed_skill(host, skill_path)
        and not _skill_assets_match(source, skill_path)
        and not _skill_assets_match(
            source,
            skill_path,
            host=host,
            python_executable=python_executable,
            os_name=os_name,
        )
    ):
        raise InstallationConflict(
            f"{skill_path} already exists but is not owned by Landing; it was not overwritten"
        )
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".landing-skill-", dir=skill_path.parent))
    backup = skill_path.parent / f".landing-backup-{uuid.uuid4().hex}"
    try:
        _copy_assets(
            source,
            staging,
            host=host,
            python_executable=python_executable,
            os_name=os_name,
        )
        _write_json(staging / MARKER, {"owner": "agent-landing", "host": host, "format": 1})
        had_existing = skill_path.exists()
        if had_existing:
            os.replace(skill_path, backup)
        try:
            os.replace(staging, skill_path)
        except Exception:
            if had_existing and backup.exists():
                os.replace(backup, skill_path)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and skill_path.exists():
            shutil.rmtree(backup)


def install_host(
    host: str,
    *,
    home: str | Path | None = None,
    python_executable: str,
    os_name: str | None = None,
) -> dict[str, Any]:
    home_path = Path(home).expanduser() if home is not None else Path.home()
    config_path, skill_path = _paths(host, home_path)
    config = _load_json(config_path)
    platform_name = os_name or os.name
    merged = _merged_config(host, config, python_executable, platform_name)
    was_installed = _is_managed_skill(host, skill_path) or any(
        _landing_command(hook.get("command"), platform=host)
        for groups in (config.get("hooks", {}) or {}).values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    )
    if host in SKILL_HOSTS:
        _install_skill(
            host,
            skill_path,
            python_executable=python_executable,
            os_name=platform_name,
        )
    else:
        # Upgrading from a version that did ship one must take it away again, or
        # the preamble it causes survives the fix that removed it.
        _remove_managed_skill(host, skill_path)
    _write_json(config_path, merged)
    return {
        "host": host,
        "status": "updated" if was_installed else "installed",
        "skill": str(skill_path) if host in SKILL_HOSTS else None,
        "config": str(config_path),
        "next_action": (
            "Open Codex, review and trust the three Landing hooks, then run landing doctor. "
            "Use 'Trust all' only if every pending hook is expected."
            if host == "codex"
            else None
        ),
    }


def _without_landing_config(host: str, config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    merged = dict(config)
    changed = False
    hooks_value = merged.get("hooks")
    if isinstance(hooks_value, dict):
        hooks = dict(hooks_value)
        for event, action in EVENT_COMMANDS.items():
            before = hooks.get(event)
            if before is None:
                continue
            after = _remove_landing_groups(before, platform=host, action=action)
            changed = changed or after != before
            if after:
                hooks[event] = after
            else:
                hooks.pop(event, None)
        if hooks:
            merged["hooks"] = hooks
        else:
            merged.pop("hooks", None)
    if host == "claude" and "statusLine" in merged:
        value = merged["statusLine"]
        command = value.get("command") if isinstance(value, dict) else None
        if _landing_command(command, action="claude-statusline"):
            wrapped = _wrapped_status_line(command)
            if wrapped:
                # Give the user's own status line back rather than removing the
                # slot: Landing borrowed it, it was never Landing's to delete.
                merged["statusLine"] = {"type": "command", "command": wrapped}
            else:
                merged.pop("statusLine")
            changed = True
    return merged, changed


def uninstall_host(host: str, *, home: str | Path | None = None) -> dict[str, Any]:
    home_path = Path(home).expanduser() if home is not None else Path.home()
    config_path, skill_path = _paths(host, home_path)
    config = _load_json(config_path)
    merged, config_changed = _without_landing_config(host, config)
    if config_changed:
        _write_json(config_path, merged)
    skill_changed = False
    if _is_managed_skill(host, skill_path):
        shutil.rmtree(skill_path)
        skill_changed = True
    return {
        "host": host,
        "status": "uninstalled" if config_changed or skill_changed else "not_installed",
        "config_changed": config_changed,
        "skill_removed": skill_changed,
    }


def _newest_reading(root: Path, provider: str) -> float | None:
    """When this provider last produced a quota reading, from state on disk.

    Read directly rather than through `StateStore`, which creates its root: a
    diagnostic that reports on state must not manufacture it.
    """
    newest: float | None = None
    latest = root / "latest_snapshot.json"
    history = root / "snapshots.jsonl"
    candidates: list[str] = []
    if latest.exists():
        try:
            candidates.append(latest.read_text(encoding="utf-8"))
        except OSError:
            pass
    if history.exists():
        try:
            candidates.extend(history.read_text(encoding="utf-8").splitlines()[-200:])
        except OSError:
            pass
    for blob in candidates:
        if not blob.strip():
            continue
        try:
            data = json.loads(blob)
            if data.get("provider") != provider:
                continue
            sampled = float(data["sampled_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if newest is None or sampled > newest:
            newest = sampled
    return newest


def _quota_source_check(host: str, state_root: Path) -> dict[str, str]:
    """Has this host's quota path ever actually delivered a number?

    Every other check proves the commands are *wired*. None of them prove a
    quota reading ever arrived -- and on a Claude Code surface that renders no
    status line (the desktop and web apps, where hooks still run fine), the
    status line is simply never invoked. The configuration then looks perfect
    while the Claude quota path is dead and the guard sits in CONSERVE / QUOTA
    UNKNOWN forever. A budget that is silently unguarded is the failure this
    project ranks below having no guard at all, so it gets its own check.

    Never reports "error": on a fresh install no reading exists yet, and Claude
    publishes quota only after the first API response of a session. Reporting a
    failure there would cry wolf on the documented `install && doctor` flow.
    """
    newest = _newest_reading(state_root, host)
    if newest is None:
        hint = (
            "Claude publishes quota only through the status line, and only after the first "
            "API response of a session. If this stays empty, the surface you are on does not "
            "render a status line and the Claude guard has no quota source."
            if host == "claude"
            else "Run `landing codex-snapshot` to confirm the app-server path works."
        )
        return {
            "name": "quota_source",
            "status": "pending",
            "detail": f"no {host} quota reading recorded in {state_root}. {hint}",
        }
    age = max(0.0, time.time() - newest)
    if age > QUOTA_SOURCE_STALE_SECONDS:
        return {
            "name": "quota_source",
            "status": "stale",
            "detail": (
                f"newest {host} quota reading is {age / 3600:.0f} hours old; the quota path "
                "may have stopped delivering."
            ),
        }
    return {
        "name": "quota_source",
        "status": "ok",
        "detail": f"newest {host} quota reading is {age / 60:.0f} minutes old",
    }


def doctor_host(
    host: str,
    *,
    home: str | Path | None = None,
    live: bool = True,
    codex_executable: str = "codex",
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    home_path = Path(home).expanduser() if home is not None else Path.home()
    # Defaults to wherever the hooks would actually read. Passed explicitly by
    # callers that need it pinned -- inferring it from `home` would quietly
    # disagree with `resolve_state_root()` whenever an override or a
    # project-scoped budget is in play.
    root = Path(state_root) if state_root is not None else resolve_state_root()
    config_path, skill_path = _paths(host, home_path)
    checks: list[dict[str, str]] = []
    if host not in SKILL_HOSTS:
        stale = _is_managed_skill(host, skill_path)
        checks.append({
            "name": "no_skill_installed",
            "status": "error" if stale else "ok",
            "detail": (
                f"a Landing skill is still installed at {skill_path}; rerun `landing install` "
                "to remove it. While it is there the model announces Landing before starting work."
                if stale
                else "Landing ships no skill for this host; the hooks do the work"
            ),
        })
    if host in SKILL_HOSTS:
        checks.append({
            "name": "managed_skill",
            "status": "ok" if _is_managed_skill(host, skill_path) else "error",
            "detail": str(skill_path),
        })
    assets_match = host not in SKILL_HOSTS or (
        _is_managed_skill(host, skill_path)
        and _skill_assets_match(
            _asset_root(host),
            skill_path,
            host=host,
            python_executable=sys.executable,
            os_name=os.name,
        )
    )
    if host in SKILL_HOSTS:
            checks.append({
            "name": "skill_content",
            "status": "ok" if assets_match else "error",
            # Name the fix. The installed skill is a *rendered* copy, so upgrading the
            # package leaves it behind, and a stale copy can silently lose plain
            # language triggering while the hooks keep enforcing -- the user then
            # believes a phrase like "leave me 70%" armed a budget when it did not.
            "detail": (
                "packaged skill is current" if assets_match
                else f"installed skill is stale or missing; re-run `landing install --host {host}` "
                     "(a package upgrade does not refresh it)"
            ),
        })
    try:
        config = _load_json(config_path)
    except InstallationConflict as error:
        checks.append({"name": "config", "status": "error", "detail": str(error)})
    else:
        hooks = config.get("hooks", {}) if isinstance(config.get("hooks", {}), dict) else {}
        configured_commands: list[str] = []
        for event, action in EVENT_COMMANDS.items():
            commands = [
                hook.get("command")
                for group in hooks.get(event, [])
                if isinstance(group, dict)
                for hook in group.get("hooks", [])
                if isinstance(hook, dict)
                and _landing_command(hook.get("command"), platform=host, action=action)
            ]
            found = bool(commands)
            configured_commands.extend(command for command in commands if isinstance(command, str))
            checks.append({
                "name": event,
                "status": "ok" if found else "error",
                "detail": "Landing hook configured" if found else "Landing hook missing",
            })
        if host == "claude":
            value = config.get("statusLine")
            command = value.get("command") if isinstance(value, dict) else None
            found = _landing_command(command, action="claude-statusline")
            if found and isinstance(command, str):
                configured_commands.append(command)
            wrapped = _wrapped_status_line(command) if found else None
            if not found:
                detail = "Landing status line missing"
            elif wrapped:
                detail = f"Landing status line configured, wrapping {wrapped!r}"
            else:
                detail = "Landing status line configured"
            checks.append({
                "name": "statusLine",
                "status": "ok" if found else "error",
                "detail": detail,
            })
        current_runtime = str(Path(sys.executable).resolve()).replace("\\", "/").casefold()
        runtime_current = bool(configured_commands) and all(
            current_runtime in command.replace("\\", "/").casefold()
            for command in configured_commands
        )
        checks.append({
            "name": "hook_runtime",
            "status": "ok" if runtime_current else "error",
            "detail": "hooks use the current Landing Python runtime" if runtime_current else "hooks point at a different or missing Python runtime; rerun landing install",
        })
    checks.append(_quota_source_check(host, root))
    if host == "codex" and live and not any(check["status"] == "error" for check in checks):
        executable = codex_executable if codex_executable != "codex" else (find_codex_executable() or "codex")
        try:
            responses = _codex_inventory(executable)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            checks.append({"name": "app_server", "status": "error", "detail": str(error)})
        else:
            hooks_result = responses[2].get("result", {})
            hook_nodes = [
                node
                for node in _walk_dicts(hooks_result)
                if node.get("source") == "user"
                and node.get("enabled") is True
                and _landing_command(node.get("command"), platform="codex")
            ]
            hook_text = json.dumps(hook_nodes)
            actions_loaded = all(action in hook_text for action in EVENT_COMMANDS.values())
            trusted = bool(hook_nodes) and all(node.get("trustStatus") == "trusted" for node in hook_nodes)
            checks.append({
                "name": "codex_hooks_loaded",
                "status": "ok" if actions_loaded else "error",
                "detail": "all three hooks loaded from user scope" if actions_loaded else "Landing user hooks were not all loaded",
            })
            checks.append({
                "name": "codex_hooks_trusted",
                "status": "ok" if trusted else "error",
                "detail": "trusted" if trusted else "not trusted; review hooks in Codex",
            })
            skills_result = responses[3].get("result", {})
            skill_nodes = [node for node in _walk_dicts(skills_result) if node.get("name") == "landing"]
            skill_ready = any(
                node.get("scope") == "user" and node.get("enabled") is True for node in skill_nodes
            )
            checks.append({
                "name": "codex_skill_loaded",
                "status": "ok" if skill_ready else "error",
                "detail": "landing skill enabled in user scope" if skill_ready else "landing user skill was not discovered",
            })
    status = "error" if any(check["status"] == "error" for check in checks) else "ok"
    return {"host": host, "status": status, "checks": checks}


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _codex_inventory(executable: str, timeout: float = 10.0) -> dict[int, dict[str, Any]]:
    process = subprocess.Popen(
        [executable, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("Could not open Codex app-server pipes")
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    responses: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    try:
        initialize = {
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "landing-doctor", "title": "Landing doctor", "version": "0.1.0"}},
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        _collect_responses(lines, responses, {1}, deadline)
        requests = [
            {"method": "initialized"},
            {"method": "hooks/list", "id": 2, "params": {}},
            {"method": "skills/list", "id": 3, "params": {"cwds": [os.getcwd()], "forceReload": True}},
        ]
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        _collect_responses(lines, responses, {2, 3}, deadline)
        return responses
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def _collect_responses(
    lines: queue.Queue[str | None],
    responses: dict[int, dict[str, Any]],
    wanted: set[int],
    deadline: float,
) -> None:
    while not wanted.issubset(responses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Timed out waiting for Codex app-server responses: {sorted(wanted)}")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as error:
            raise RuntimeError(f"Timed out waiting for Codex app-server responses: {sorted(wanted)}") from error
        if line is None:
            raise RuntimeError("Codex app-server exited before inventory completed")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        response_id = payload.get("id")
        if isinstance(response_id, int):
            responses[response_id] = payload
