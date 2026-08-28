"""Sharing Claude Code's single statusLine slot.

Claude Code exposes `rate_limits` to the status line and nowhere else, so that
one slot is Landing's only quota source on Claude. Refusing to share it locked
out exactly the users who already run a status line -- the same people who want
a quota budget. Landing ingests quota from the payload, then runs the user's own
command with the same payload and prints its output.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agent_landing.cli import build_parser
from agent_landing.state import StateStore

PAYLOAD = {"rate_limits": {"five_hour": {"used_percentage": 40}}}


def run_status_line(tmp_path: Path, monkeypatch, wrap: str | None) -> str:
    argv = ["--cwd", str(tmp_path), "claude-statusline"]
    if wrap is not None:
        argv += ["--wrap", wrap]
    args = build_parser().parse_args(argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(PAYLOAD)))
    assert args.func(args) == 0
    return ""


def test_without_a_wrapper_landing_prints_its_own_line(tmp_path, monkeypatch, capsys):
    run_status_line(tmp_path, monkeypatch, wrap=None)
    assert "five_hour 60.0% left" in capsys.readouterr().out


def test_the_users_own_status_line_output_is_forwarded(tmp_path, monkeypatch, capsys):
    run_status_line(tmp_path, monkeypatch, wrap="echo MINE")
    out = capsys.readouterr().out
    assert "MINE" in out
    assert "five_hour" not in out, "Landing must not append to the user's status line"


def test_quota_is_still_ingested_while_wrapping(tmp_path, monkeypatch, capsys):
    """The whole point of the slot: wrapping must not cost the quota reading."""
    run_status_line(tmp_path, monkeypatch, wrap="echo MINE")
    capsys.readouterr()

    snapshot = StateStore(tmp_path).load_snapshot()
    assert snapshot.window("five_hour").remaining_percent == 60.0


def test_the_wrapped_command_receives_the_same_payload(tmp_path, monkeypatch, capsys):
    """It was written against Claude's payload, so it has to be given Claude's payload."""
    script = tmp_path / "mine.py"
    script.write_text(
        "import json, sys\n"
        "print('used=' + str(json.load(sys.stdin)['rate_limits']['five_hour']['used_percentage']))\n",
        encoding="utf-8",
    )
    run_status_line(tmp_path, monkeypatch, wrap=f'"{sys.executable}" "{script}"')
    assert "used=40" in capsys.readouterr().out


def test_a_failing_wrapped_command_falls_back_to_landings_line(tmp_path, monkeypatch, capsys):
    """An empty status bar would read as Landing having broken the user's."""
    run_status_line(tmp_path, monkeypatch, wrap="exit 7")
    captured = capsys.readouterr()
    assert "five_hour 60.0% left" in captured.out
    assert "wrapped status line exited" in captured.err


def _run_cli(tmp_path: Path, payload: dict, *, io_encoding: str, wrap: str | None = None) -> bytes:
    """Run the real command in a child process with a forced stdout code page."""
    import os
    import subprocess

    env = dict(os.environ, PYTHONIOENCODING=io_encoding, LANDING_HOME=str(tmp_path))
    argv = [sys.executable, "-m", "agent_landing.cli", "claude-statusline"]
    if wrap is not None:
        argv += ["--wrap", wrap]
    result = subprocess.run(
        argv, input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def test_the_status_line_is_utf8_whatever_the_console_code_page_is(tmp_path):
    """Claude Code reads this channel as UTF-8; Python defaults to the locale.

    On cp949 the separator went out as 0xA1 0xA4 and Claude Code rendered
    mojibake in the status bar. A code page missing the character -- an em dash
    on cp949, say -- would have raised instead, blanking the bar.
    """
    out = _run_cli(
        tmp_path,
        {"rate_limits": {"five_hour": {"used_percentage": 40}, "seven_day": {"used_percentage": 10}}},
        io_encoding="cp949",
    )

    assert "\u00b7".encode("utf-8") in out
    assert "\u00b7".encode("cp949") not in out
    assert "five_hour 60.0% left" in out.decode("utf-8")


def test_a_wrapped_status_line_that_emits_utf8_is_forwarded_intact(tmp_path):
    """`text=True` alone decoded the child with the console code page."""
    script = tmp_path / "mine.py"
    script.write_text(
        "import sys\nsys.stdout.reconfigure(encoding='utf-8')\nprint('\u2014 \uc548\ub155')\n",
        encoding="utf-8",
    )
    wrap = f'"{sys.executable}" "{script}"'

    out = _run_cli(tmp_path, PAYLOAD, io_encoding="cp949", wrap=wrap)

    assert "\u2014 \uc548\ub155" in out.decode("utf-8")
