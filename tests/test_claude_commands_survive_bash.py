"""Claude Code runs hook and status-line commands through Git Bash on Windows.

Observed 2026-10-15 in the terminal CLI 2.1.211: every hook failed with
`C:UsersuserDesktopLanding.venvScriptspython.exe: command not found`. Bash
had eaten the backslashes in the path the installer wrote. Claude Code
reports that as a *non-blocking* hook error, so the gate was silently open,
and the status line (the only Claude quota source) never ran either.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_landing.installation import EVENT_COMMANDS, doctor_host, install_host


def _claude_commands(home: Path) -> list[str]:
    config = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = [hook["command"] for event in EVENT_COMMANDS for group in config["hooks"][event] for hook in group["hooks"]]
    return [*hooks, config["statusLine"]["command"]]


def test_windows_commands_name_python_with_forward_slashes(tmp_path):
    install_host("claude", home=tmp_path, python_executable=r"C:\Users\user\Landing\.venv\Scripts\python.exe",
                 os_name="nt")
    for command in _claude_commands(tmp_path):
        assert command.startswith("C:/Users/user/Landing/.venv/Scripts/python.exe -m agent_landing.cli ")


def test_a_path_with_spaces_is_double_quoted(tmp_path):
    install_host("claude", home=tmp_path, python_executable=r"C:\Program Files\Py\python.exe", os_name="nt")
    for command in _claude_commands(tmp_path):
        assert command.startswith('"C:/Program Files/Py/python.exe" -m agent_landing.cli ')


def test_codex_commands_are_unchanged(tmp_path):
    """Codex binds hook trust to the command string; changing it untrusts every hook."""
    install_host("codex", home=tmp_path, python_executable=r"C:\Py\python.exe", os_name="nt")
    text = (tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert "C:\\\\Py\\\\python.exe" in text


@pytest.mark.skipif(os.name != "nt" or not shutil.which("bash"), reason="needs Windows with Git Bash")
def test_the_installed_commands_actually_run_under_bash(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name="nt")
    env = {**os.environ, "LANDING_HOME": str(tmp_path / "landing")}
    for command in _claude_commands(tmp_path):
        result = subprocess.run(["bash", "-c", command], input=b"{}", capture_output=True, env=env, timeout=60)
        assert result.returncode != 127, result.stderr.decode("utf-8", "replace")
        assert b"command not found" not in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="the check exists only on Windows, where Claude Code uses Git Bash")
def test_doctor_flags_an_install_bash_would_break(tmp_path):
    install_host("claude", home=tmp_path, python_executable=sys.executable, os_name="nt")
    settings = tmp_path / ".claude" / "settings.json"
    config = json.loads(settings.read_text(encoding="utf-8"))
    for event in EVENT_COMMANDS:
        for group in config["hooks"][event]:
            for hook in group["hooks"]:
                hook["command"] = hook["command"].replace("/", "\\", 3)
    settings.write_text(json.dumps(config), encoding="utf-8")

    check = next(c for c in doctor_host("claude", home=tmp_path, live=False)["checks"] if c["name"] == "bash_safe_paths")
    assert check["status"] == "error"
    assert "landing install" in check["detail"]


def test_a_claude_refusal_without_quota_says_how_to_get_some(tmp_path):
    """Claude Code hands quota to the status line only after its first reply, so
    `/landing` as a conversation's first message always finds none. Observed
    2026-10-15 on 2.1.211. Without this hint the refusal reads as a broken install."""
    from agent_landing.hooks import user_prompt
    from agent_landing.state import StateStore

    result = user_prompt({"prompt": "/landing spend 15 x", "session_id": "s", "cwd": str(tmp_path)},
                         StateStore(tmp_path, session_id="s"), provider="claude")
    assert "send any ordinary message first" in json.dumps(result)
