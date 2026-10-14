from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_landing.cli import build_parser
from agent_landing.installation import (
    EVENT_COMMANDS,
    InstallationConflict,
    doctor_host,
    install_host,
    uninstall_host,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(config: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in config["hooks"][event]
        for hook in group.get("hooks", [])
    ]


def test_codex_install_is_global_portable_and_preserves_unrelated_hooks(tmp_path):
    config_path = tmp_path / ".codex" / "hooks.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "description": "mine",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "read_file", "hooks": [{"type": "command", "command": "audit"}]}
                    ]
                },
                "extra": {"keep": True},
            }
        ),
        encoding="utf-8",
    )

    result = install_host(
        "codex",
        home=tmp_path,
        python_executable="C:/Python/python.exe",
        os_name="nt",
    )
    config = _read(config_path)

    assert result["status"] == "installed"
    assert config["description"] == "mine"
    assert config["extra"] == {"keep": True}
    assert "audit" in _commands(config, "PreToolUse")
    for event in ("PreToolUse", "UserPromptSubmit", "Stop"):
        landing = [command for command in _commands(config, event) if "agent_landing.cli" in command]
        assert len(landing) == 1
        assert "C:/Python/python.exe" in landing[0]
    # No codex path is baked in; see the trust-stability test below for why.
    assert not any("Codex" in command for command in _commands(config, "PreToolUse"))
    # Codex gets no skill. Being one made the model announce Landing before any
    # work began, and the hooks open the budget without it.
    assert not (tmp_path / ".agents" / "skills" / "landing").exists()
    assert result["skill"] is None


def test_installing_codex_takes_away_a_skill_an_older_version_left(tmp_path):
    """The preamble survives the fix if the skill that causes it survives."""
    skill = tmp_path / ".agents" / "skills" / "landing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("stale", encoding="utf-8")
    (skill / ".landing-install.json").write_text(
        json.dumps({"owner": "agent-landing", "host": "codex", "format": 1}), encoding="utf-8"
    )

    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")
    assert not skill.exists()


def test_a_skill_landing_does_not_own_is_left_alone_on_codex(tmp_path):
    skill = tmp_path / ".agents" / "skills" / "landing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("not Landing", encoding="utf-8")

    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "not Landing"


def test_codex_reinstall_replaces_old_landing_entries_without_duplicates(tmp_path):
    install_host("codex", home=tmp_path, python_executable="C:/Old/python.exe", os_name="nt")
    result = install_host("codex", home=tmp_path, python_executable="C:/New/python.exe", os_name="nt")
    config = _read(tmp_path / ".codex" / "hooks.json")

    assert result["status"] == "updated"
    for event in ("PreToolUse", "UserPromptSubmit", "Stop"):
        landing = [command for command in _commands(config, event) if "agent_landing.cli" in command]
        assert len(landing) == 1
        assert "C:/New/python.exe" in landing[0]
        assert "C:/Old/python.exe" not in landing[0]


def test_claude_install_merges_settings_and_installs_user_skill(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": []}]}}),
        encoding="utf-8",
    )

    install_host("claude", home=tmp_path, python_executable="/opt/landing/bin/python", os_name="posix")
    settings = _read(settings_path)

    assert settings["permissions"] == {"allow": ["Read"]}
    assert "Stop" in settings["hooks"]
    assert settings["statusLine"]["command"].startswith("/opt/landing/bin/python -m agent_landing.cli")
    for event in ("PreToolUse", "UserPromptSubmit", "Stop"):
        assert len([c for c in _commands(settings, event) if "agent_landing.cli" in c]) == 1
    assert (tmp_path / ".claude" / "skills" / "landing" / "SKILL.md").is_file()


def _with_status_line(tmp_path: Path, command: str) -> Path:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(exist_ok=True)
    settings_path.write_text(
        json.dumps({"statusLine": {"type": "command", "command": command}}), encoding="utf-8"
    )
    return settings_path


def test_an_existing_status_line_is_wrapped_rather_than_taken(tmp_path):
    """Claude Code has one statusLine slot and reports quota only there.

    Refusing to share it locked out precisely the users who already run a status
    line -- the same people who want a quota budget in the first place.
    """
    settings_path = _with_status_line(tmp_path, "my-status --fancy")
    install_host("claude", home=tmp_path, python_executable="python", os_name="posix")

    command = _read(settings_path)["statusLine"]["command"]
    assert "agent_landing.cli claude-statusline" in command
    assert "--wrap 'my-status --fancy'" in command


def test_uninstall_gives_a_wrapped_status_line_back(tmp_path):
    """Landing borrowed the slot; it was never Landing's to delete."""
    settings_path = _with_status_line(tmp_path, "my-status --fancy")
    install_host("claude", home=tmp_path, python_executable="python", os_name="posix")
    uninstall_host("claude", home=tmp_path)

    assert _read(settings_path)["statusLine"] == {"type": "command", "command": "my-status --fancy"}


def test_reinstalling_keeps_wrapping_the_same_command(tmp_path):
    """`install` is also the update command, so it must not eat its own wrapper."""
    settings_path = _with_status_line(tmp_path, "my-status")
    install_host("claude", home=tmp_path, python_executable="python", os_name="posix")
    install_host("claude", home=tmp_path, python_executable="/new/python", os_name="posix")

    command = _read(settings_path)["statusLine"]["command"]
    assert command.startswith("/new/python")
    assert command.count("--wrap") == 1
    assert "--wrap my-status" in command


def test_a_status_line_that_cannot_be_wrapped_safely_is_refused(tmp_path):
    """A mangled status line is a mangled quota source; refuse rather than guess."""
    settings_path = _with_status_line(tmp_path, 'sh -c "echo hi"')
    original = _read(settings_path)

    with pytest.raises(InstallationConflict, match="double quote"):
        install_host("claude", home=tmp_path, python_executable="python", os_name="posix")

    assert _read(settings_path) == original
    assert not (tmp_path / ".claude" / "skills" / "landing").exists()


def test_doctor_names_the_command_being_wrapped(tmp_path):
    _with_status_line(tmp_path, "my-status")
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    report = doctor_host("claude", home=tmp_path, live=False)

    status_line = next(c for c in report["checks"] if c["name"] == "statusLine")
    assert status_line["status"] == "ok"
    assert "my-status" in status_line["detail"]


def test_the_code_fence_is_converted_whatever_the_assets_line_endings(tmp_path):
    """A CRLF asset must not silently ship a skill telling Windows users to run bash.

    The conversion used to be a literal "```bash\\n..." match, which simply found
    nothing on a CRLF asset and reported success.
    """
    from agent_landing.installation import _render_skill_asset

    for newline in ("\n", "\r\n"):
        asset = f"```bash{newline}landing invoke \"$ARGUMENTS\" --provider codex{newline}```{newline}"
        rendered = _render_skill_asset(
            asset.encode("utf-8"), host="codex", python_executable="C:/py/python.exe", os_name="nt"
        ).decode("utf-8")
        assert "```powershell" in rendered, repr(newline)
        assert "```bash" not in rendered, repr(newline)


def test_the_runtime_prefix_stays_out_of_prose(tmp_path):
    """The prefix makes a runnable command correct; it makes a sentence unreadable.

    It also used to apply unevenly -- a prose mention that wrapped onto the next
    line was skipped -- so one clause named an absolute interpreter path and the
    next said plain `landing`, in the same sentence.
    """
    from agent_landing.installation import _render_skill_asset

    asset = chr(10).join([
        "do not run `landing invoke` or `landing",
        "start` yourself.",
        "",
        "```bash",
        'landing invoke "$ARGUMENTS" --provider claude',
        "```",
        "",
    ])
    rendered = _render_skill_asset(
        asset.encode("utf-8"), host="claude", python_executable="/opt/py", os_name="posix"
    ).decode("utf-8")

    prose, fence = rendered.split("```bash", 1)
    assert "`landing invoke`" in prose, "prose must keep the short name"
    assert "/opt/py" not in prose
    assert "/opt/py -m agent_landing.cli invoke" in fence, "the runnable command must be exact"


def _record_reading(home: Path, provider: str, age_seconds: float) -> None:
    """Write the state a working quota path would have left behind."""
    root = home / ".landing"
    root.mkdir(parents=True, exist_ok=True)
    (root / "latest_snapshot.json").write_text(
        json.dumps({
            "provider": provider,
            "windows": [{"id": "five_hour", "used_percent": 20.0, "remaining_percent": 80.0}],
            "sampled_at": time.time() - age_seconds,
            "unreadable_windows": [],
        }),
        encoding="utf-8",
    )


def _doctor(home: Path) -> dict:
    """Doctor pinned at the fake home's state root, never the developer's real one."""
    return doctor_host("claude", home=home, live=False, state_root=home / ".landing")


def _quota_source(report: dict) -> dict:
    return next(c for c in report["checks"] if c["name"] == "quota_source")


def test_doctor_reports_a_quota_path_that_has_never_delivered(tmp_path):
    """Wired is not the same as working.

    On a Claude Code surface that renders no status line the hooks still run, so
    every configuration check passes while the quota path is dead. That state has
    to be visible, or the budget is silently unguarded.
    """
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    report = _doctor(tmp_path)

    check = _quota_source(report)
    assert check["status"] == "pending"
    assert "status line" in check["detail"]
    # Never an error: a fresh install legitimately has no reading yet.
    assert report["status"] == "ok"


def test_doctor_is_satisfied_by_a_recent_reading(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    _record_reading(tmp_path, "claude", age_seconds=120)
    assert _quota_source(_doctor(tmp_path))["status"] == "ok"


def test_doctor_flags_a_quota_path_that_stopped_delivering(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    _record_reading(tmp_path, "claude", age_seconds=3 * 24 * 3600)

    report = _doctor(tmp_path)
    assert _quota_source(report)["status"] == "stale"
    # Surfaced, but not a failure: the install itself is still correct.
    assert report["status"] == "ok"


def test_one_hosts_readings_do_not_vouch_for_the_other(tmp_path):
    """Both hosts share one state root, so the check must match on provider."""
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    _record_reading(tmp_path, "codex", age_seconds=60)
    assert _quota_source(_doctor(tmp_path))["status"] == "pending"


def test_doctor_does_not_create_the_state_directory(tmp_path):
    """A diagnostic that reports on state must not manufacture it."""
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    _doctor(tmp_path)
    assert not (tmp_path / ".landing").exists()


def test_invalid_existing_json_is_never_overwritten(tmp_path):
    config_path = tmp_path / ".codex" / "hooks.json"
    config_path.parent.mkdir()
    config_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(InstallationConflict, match="valid JSON"):
        install_host("codex", home=tmp_path, python_executable="python", os_name="posix")

    assert config_path.read_text(encoding="utf-8") == "{broken"
    assert not (tmp_path / ".agents" / "skills" / "landing").exists()


def test_uninstall_removes_only_landing_owned_content(tmp_path):
    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")
    config_path = tmp_path / ".codex" / "hooks.json"
    config = _read(config_path)
    config["hooks"]["PreToolUse"].insert(
        0, {"matcher": "read_file", "hooks": [{"type": "command", "command": "keep-me"}]}
    )
    config["custom"] = 7
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = uninstall_host("codex", home=tmp_path)
    remaining = _read(config_path)

    assert result["status"] == "uninstalled"
    assert remaining["custom"] == 7
    assert _commands(remaining, "PreToolUse") == ["keep-me"]
    assert "PostToolUse" not in remaining["hooks"]
    assert "UserPromptSubmit" not in remaining["hooks"]
    assert not (tmp_path / ".agents" / "skills" / "landing").exists()


def test_uninstall_does_not_delete_unowned_skill_directory(tmp_path):
    skill = tmp_path / ".agents" / "skills" / "landing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine", encoding="utf-8")

    result = uninstall_host("codex", home=tmp_path)

    assert result["status"] == "not_installed"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_fake_ownership_marker_neither_allows_overwrite_nor_deletion(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "landing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine", encoding="utf-8")
    (skill / ".landing-install.json").write_text(
        json.dumps({"owner": "someone-else", "host": "claude", "format": 1}),
        encoding="utf-8",
    )

    with pytest.raises(InstallationConflict, match="not owned"):
        install_host("claude", home=tmp_path, python_executable="python", os_name="posix")
    result = uninstall_host("claude", home=tmp_path)

    assert result["status"] == "not_installed"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_manual_skill_with_extra_files_is_not_adopted(tmp_path):
    source = Path(__file__).parents[1] / "src" / "agent_landing" / "assets" / "claude" / "landing"
    skill = tmp_path / ".claude" / "skills" / "landing"
    skill.parent.mkdir(parents=True)
    shutil.copytree(source, skill)
    (skill / "extra.txt").write_text("not Landing", encoding="utf-8")

    with pytest.raises(InstallationConflict, match="not owned"):
        install_host("claude", home=tmp_path, python_executable="python", os_name="posix")


def test_install_adopts_an_identical_manual_skill_copy(tmp_path):
    source = Path(__file__).parents[1] / "src" / "agent_landing" / "assets" / "claude" / "landing"
    skill = tmp_path / ".claude" / "skills" / "landing"
    skill.parent.mkdir(parents=True)
    shutil.copytree(source, skill)

    result = install_host("claude", home=tmp_path, python_executable="python", os_name="posix")

    assert result["status"] == "installed"
    assert (skill / ".landing-install.json").is_file()


def test_doctor_reports_complete_static_install(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)

    result = doctor_host("claude", home=tmp_path, live=False)

    assert result["status"] == "ok"
    # `quota_source` is excluded on purpose: it reports whether a quota reading
    # has ever arrived, which a just-installed host cannot have yet. Every check
    # that is actually about the installation must be ok.
    install_checks = [c for c in result["checks"] if c["name"] != "quota_source"]
    assert all(check["status"] == "ok" for check in install_checks)


def test_doctor_reports_a_stale_installed_skill(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    (tmp_path / ".claude" / "skills" / "landing" / "SKILL.md").write_text(
        "stale", encoding="utf-8"
    )

    result = doctor_host("claude", home=tmp_path, live=False)

    assert result["status"] == "error"
    assert any(check["name"] == "skill_content" and check["status"] == "error" for check in result["checks"])


def test_doctor_reports_hooks_that_point_at_an_old_python_runtime(tmp_path):
    install_host("claude", home=tmp_path, python_executable="C:/old/python.exe", os_name=os.name)

    result = doctor_host("claude", home=tmp_path, live=False)

    assert result["status"] == "error"
    assert any(check["name"] == "hook_runtime" and check["status"] == "error" for check in result["checks"])


def test_cli_install_doctor_and_uninstall_use_the_requested_home(tmp_path, capsys):
    parser = build_parser()
    install_args = parser.parse_args(["install", "--host", "claude", "--home", str(tmp_path)])
    assert install_args.func(install_args) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["results"][0]["status"] == "installed"
    # Never an unqualified "ready": on Codex the hooks are untrusted until
    # reviewed, and untrusted hooks do not run.
    assert installed["message"].startswith("Installed, but NOT ENFORCING YET:")
    assert "Restart them" in installed["message"], "this run installs claude only"

    doctor_args = parser.parse_args(
        ["doctor", "--host", "claude", "--home", str(tmp_path), "--no-live"]
    )
    assert doctor_args.func(doctor_args) == 0
    diagnosed = json.loads(capsys.readouterr().out)
    assert diagnosed["results"][0]["status"] == "ok"

    uninstall_args = parser.parse_args(["uninstall", "--host", "claude", "--home", str(tmp_path)])
    assert uninstall_args.func(uninstall_args) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["results"][0]["status"] == "uninstalled"


def test_packaged_skills_match_the_repository_host_skills():
    root = Path(__file__).parents[1]
    pairs = [
        (
            root / ".claude" / "skills" / "landing" / "SKILL.md",
            root / "src" / "agent_landing" / "assets" / "claude" / "landing" / "SKILL.md",
        ),
    ]
    for source, packaged in pairs:
        assert packaged.read_text(encoding="utf-8").splitlines() == source.read_text(
            encoding="utf-8"
        ).splitlines()


def test_the_codex_hook_command_survives_a_codex_update(tmp_path):
    """The installed command must not contain the Codex binary path.

    Codex binds hook trust to the command string, and on Windows the binary
    lives under a content-hashed directory that changes on every update. Baking
    the path in meant each update rewrote the hash, invalidated the trust, and
    reverted PreToolUse -- the only hook that actually blocks -- to untrusted
    until someone re-approved it. Observed twice in two days.

    Installing twice with different Codex locations must produce byte-identical
    configuration.
    """
    import os

    def install_with_codex_at(location: str) -> str:
        home = tmp_path / location.replace("/", "_")
        os.environ["PATH"] = location
        install_host("codex", home=home, python_executable="C:/Python/python.exe", os_name="nt")
        return (home / ".codex" / "hooks.json").read_text(encoding="utf-8")

    before = os.environ.get("PATH", "")
    try:
        first = install_with_codex_at("C:/Codex/bin/aaaaaaaa")
        second = install_with_codex_at("C:/Codex/bin/bbbbbbbb")
    finally:
        os.environ["PATH"] = before

    assert first == second
    assert "aaaaaaaa" not in first and "bbbbbbbb" not in first


def test_install_does_not_offer_a_flag_it_would_ignore():
    """`--codex-executable` on `install` became a silent no-op; it was removed.

    A flag that is accepted and ignored is the same class of failure as a hook
    that looks configured and never runs.
    """
    from agent_landing.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["install", "--codex-executable", "C:/Codex/codex.exe"])
    # It remains on `doctor`, which genuinely probes the live binary.
    args = build_parser().parse_args(["doctor", "--codex-executable", "C:/Codex/codex.exe"])
    assert args.codex_executable == "C:/Codex/codex.exe"


def test_install_never_reports_ready_while_the_guard_is_disarmed(tmp_path, capsys):
    """Writing the files is not the same as arming them.

    Codex binds hook trust to the hook file's hash, so every install -- an
    *update* included -- leaves the hooks untrusted, and untrusted hooks never
    run. Observed live on 2026-08-28: after reinstalling, a budget sat in LAND
    while Codex edited source and ran shell commands unchallenged, because the
    hooks were inert. An installer that says "ready" there manufactures the
    silently-unguarded state the guard exists to prevent.
    """
    args = build_parser().parse_args(["install", "--host", "codex", "--home", str(tmp_path)])
    assert args.func(args) == 0
    message = json.loads(capsys.readouterr().out)["message"]

    assert "NOT ENFORCING YET" in message
    assert "Landing is ready" not in message
    assert "codex_hooks_trusted" in message, "name the check that confirms it"


def test_claude_install_warns_that_running_sessions_keep_old_hooks(tmp_path):
    result = install_host(
        "claude", home=tmp_path, python_executable="python", os_name="posix"
    )
    assert "Restart them" in result["next_action"]


def _mark(skill: Path, host: str) -> None:
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("stale", encoding="utf-8")
    (skill / ".landing-install.json").write_text(
        json.dumps({"owner": "agent-landing", "host": host, "format": 1}), encoding="utf-8"
    )


def test_codex_skill_marked_with_another_host_is_still_removed(tmp_path):
    """Found live on 2026-08-29 in ~/.agents/skills/landing.

    Its marker said `host: claude`, so the exact-match ownership test refused
    it, and every `landing install` left it in place -- the one directory the
    removal exists to clean up. The path decides the host; no Claude install
    writes the Codex slot, so a marker naming the other host there is corrupt
    data, not a foreign owner.
    """
    skill = tmp_path / ".agents" / "skills" / "landing"
    _mark(skill, "claude")

    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")

    assert not skill.exists()


def test_a_skill_another_owner_marked_is_left_alone_on_codex(tmp_path):
    """Loosening the host must not loosen the owner."""
    skill = tmp_path / ".agents" / "skills" / "landing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("not Landing", encoding="utf-8")
    (skill / ".landing-install.json").write_text(
        json.dumps({"owner": "someone-else", "host": "codex", "format": 1}), encoding="utf-8"
    )

    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")

    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "not Landing"


def test_doctor_reports_a_stale_codex_skill_whatever_host_it_names(tmp_path):
    install_host("codex", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    _mark(tmp_path / ".agents" / "skills" / "landing", "claude")

    result = doctor_host("codex", home=tmp_path, live=False)
    check = next(c for c in result["checks"] if c["name"] == "no_skill_installed")

    assert result["status"] == "error"
    assert check["status"] == "error"
    assert "landing install" in check["detail"]


def _inventory(home: Path, *, trusted: bool = True, skill: bool = True) -> dict:
    """What `hooks/list` and `skills/list` would report for this install.

    Built from the hooks Landing actually wrote, so the fake cannot drift away
    from the commands the recognizer looks for.
    """
    config = _read(home / ".codex" / "hooks.json")
    hooks = [
        {
            "source": "user",
            "enabled": True,
            "command": command,
            "trustStatus": "trusted" if trusted else "untrusted",
        }
        for event in EVENT_COMMANDS
        for command in _commands(config, event)
    ]
    skills = [{"name": "landing", "scope": "user", "enabled": True}] if skill else []
    return {2: {"result": {"hooks": hooks}}, 3: {"result": {"skills": skills}}}


def test_doctor_calls_a_landing_skill_loaded_in_codex_an_error(tmp_path, monkeypatch):
    """The live half of `no_skill_installed`.

    A skill loaded from somewhere Landing does not manage is invisible to the
    filesystem check, and it brings back the preamble just the same. Reporting
    it as `ok` -- which it did -- let doctor go fully green on the exact state
    the Codex skill was deleted to prevent.
    """
    import agent_landing.installation as installation

    install_host("codex", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    monkeypatch.setattr(installation, "_codex_inventory", lambda *a, **k: _inventory(tmp_path))

    result = doctor_host("codex", home=tmp_path, live=True)
    check = next(c for c in result["checks"] if c["name"] == "codex_skill_absent")

    assert check["status"] == "error"
    assert result["status"] == "error"


def test_doctor_is_green_when_codex_loads_hooks_and_no_skill(tmp_path, monkeypatch):
    import agent_landing.installation as installation

    install_host("codex", home=tmp_path, python_executable=sys.executable, os_name=os.name)
    monkeypatch.setattr(installation, "_codex_inventory", lambda *a, **k: _inventory(tmp_path, skill=False))

    result = doctor_host("codex", home=tmp_path, live=True)
    names = {c["name"]: c["status"] for c in result["checks"]}

    assert names["codex_skill_absent"] == "ok"
    assert names["codex_hooks_trusted"] == "ok"


def test_reinstalling_removes_the_retired_post_tool_hook(tmp_path):
    """The handoff no longer lands through a write, so the hook that watched for
    that write is gone. An upgrade must take Landing's old entry away -- and only
    Landing's -- or every tool call keeps paying for a process that does nothing."""
    config_path = tmp_path / ".codex" / "hooks.json"
    config_path.parent.mkdir()
    old = "python -m agent_landing.cli hook-post-tool --platform codex"
    config_path.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": old}]},
        {"matcher": "*", "hooks": [{"type": "command", "command": "keep-me"}]},
    ]}}), encoding="utf-8")

    install_host("codex", home=tmp_path, python_executable="python", os_name="posix")
    config = _read(config_path)

    assert _commands(config, "PostToolUse") == ["keep-me"]
    assert any("hook-stop" in c for c in _commands(config, "Stop"))


def test_the_claude_stop_hook_sits_beside_the_user_s_own(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "notify-me"}]},
    ]}}), encoding="utf-8")

    install_host("claude", home=tmp_path, python_executable="python", os_name="posix")
    commands = _commands(_read(settings_path), "Stop")
    assert "notify-me" in commands
    assert len([c for c in commands if "hook-stop" in c]) == 1
