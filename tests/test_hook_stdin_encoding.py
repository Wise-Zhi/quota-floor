"""Hosts send hook payloads as UTF-8 whatever the console code page is.

Observed 2026-10-15 in ChatGPT's Work mode (Codex runtime) on a cp949 machine:
`json.load(sys.stdin)` decoded the payload with the locale, so Chinese arrived
as lone surrogates. The task name was garbage, and the first place that
re-encoded it raised -- the gate then failed closed and blocked every tool,
`landing status` included. Chinese keywords (留/花) could never have parsed,
and the Stop hook would have saved a garbled handoff.
"""

import io
import json
import sys

import pytest

from agent_landing.cli import build_parser, read_hook_payload


class _Stdin(io.TextIOWrapper):
    """A text stdin decoding with the wrong code page, as on a cp949 console."""


def _stdin(data: bytes, encoding: str = "cp949") -> io.TextIOWrapper:
    return _Stdin(io.BytesIO(data), encoding=encoding, errors="surrogateescape")


PAYLOAD = {"prompt": "$landing 留 20 给 src/fib.py 加一行 docstring", "session_id": "s"}


def test_a_utf8_payload_is_read_as_utf8_on_a_cp949_console(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _stdin(json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")))
    assert read_hook_payload() == PAYLOAD


def test_a_byte_order_mark_is_tolerated(monkeypatch):
    data = b"\xef\xbb\xbf" + json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", _stdin(data))
    assert read_hook_payload() == PAYLOAD


def test_a_payload_in_the_local_code_page_still_reads(monkeypatch):
    """Not what hosts send, but better read than refused."""
    data = json.dumps({"prompt": "한국어"}, ensure_ascii=False).encode("cp949")
    monkeypatch.setattr(sys, "stdin", _stdin(data))
    assert read_hook_payload() == {"prompt": "한국어"}


@pytest.mark.parametrize("command", ["hook-user-prompt", "hook-pre-tool", "hook-stop", "hook-post-tool"])
def test_every_hook_command_reads_through_it(command, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANDING_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", _stdin(json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")))
    seen = {}
    import agent_landing.cli as cli

    original = cli.read_hook_payload

    def spy():
        seen["payload"] = original()
        return seen["payload"]

    monkeypatch.setattr(cli, "read_hook_payload", spy)
    args = build_parser().parse_args([command, "--platform", "codex"])
    if hasattr(args, "refresh_after_seconds"):
        args.refresh_after_seconds = 1e9
    assert args.func(args) == 0
    assert seen["payload"] == PAYLOAD


def test_the_hook_records_a_chinese_task_intact(tmp_path, monkeypatch, capsys):
    from time import time

    from agent_landing.models import QuotaSnapshot, QuotaWindow
    from agent_landing.state import StateStore

    monkeypatch.setenv("LANDING_HOME", str(tmp_path))
    StateStore().save_snapshot(QuotaSnapshot(
        provider="codex", windows=[QuotaWindow(id="seven_day", used_percent=60)], sampled_at=time(),
    ))
    monkeypatch.setattr(sys, "stdin", _stdin(json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")))
    args = build_parser().parse_args(["hook-user-prompt", "--platform", "codex", "--refresh-after-seconds", "1e9"])
    assert args.func(args) == 0

    session = StateStore(session_id="s").load_session()
    assert (session.mode, session.task) == ("preserve", "给 src/fib.py 加一行 docstring")


def test_a_prompt_that_opens_a_budget_reads_quota_afresh(tmp_path, monkeypatch):
    """The baseline a `spend` is measured from must not be a minute old."""
    import agent_landing.cli as cli

    monkeypatch.setenv("LANDING_HOME", str(tmp_path))
    ages = []
    monkeypatch.setattr(cli, "_refresh_codex_if_needed", lambda store, max_age, exe: ages.append(max_age))
    for prompt in ("$landing spend 10 fix it", "just a normal message"):
        monkeypatch.setattr(sys, "stdin", _stdin(json.dumps({"prompt": prompt}).encode("utf-8")))
        args = build_parser().parse_args(["hook-user-prompt", "--platform", "codex"])
        args.func(args)
    assert ages[0] == 0.0
    assert ages[1] == 60.0
