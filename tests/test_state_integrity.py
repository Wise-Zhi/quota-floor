"""What the gate does when its own state file is damaged.

A hook that raises exits non-zero, and Claude Code treats every exit code other
than 2 as a non-blocking error: the tool then runs anyway. So a crash inside the
gate does not merely break a feature, it silently removes the guard while the
user still believes they are protected. These tests pin the fail-closed
behaviour, and the way back out of it.
"""

import json
import sys
from pathlib import Path
from time import time

import pytest

from agent_landing.cli import build_parser, main
from agent_landing.hooks import gate_failure, post_tool, pre_tool, user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import CorruptStateError, StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}


def store_with_session(tmp_path: Path, remaining: float = 91.0) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="implement auth",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=95,
        cwd=str(tmp_path),
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    return store


def truncate_session(store: StateStore) -> None:
    """Exactly what an interrupted write used to leave behind."""
    store.session_path.write_text('{"task": "implement au', encoding="utf-8")


def test_a_damaged_session_is_not_mistaken_for_no_session(tmp_path):
    store = store_with_session(tmp_path)
    truncate_session(store)
    with pytest.raises(CorruptStateError):
        store.load_session()


def test_the_gate_denies_rather_than_crashing_on_a_damaged_session(tmp_path):
    store = store_with_session(tmp_path)
    truncate_session(store)
    result = pre_tool(WRITE, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    # A deny with no exit is its own trap, so the way out has to be in the text.
    assert "landing cancel" in reason


def test_an_internal_failure_with_no_budget_requested_does_not_block(tmp_path):
    """No session means no mandate; a bug must not block work that was never guarded."""
    store = StateStore(tmp_path)
    assert gate_failure(store, RuntimeError("boom")) == {}


def test_an_internal_failure_with_a_budget_in_force_blocks(tmp_path):
    store = store_with_session(tmp_path)
    result = gate_failure(store, RuntimeError("boom"))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "boom" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_session_ending_hooks_fail_safe_instead_of_closed(tmp_path):
    """These two only ever stop or renew a budget, so doing nothing is the safe answer."""
    store = store_with_session(tmp_path)
    truncate_session(store)
    with pytest.raises(CorruptStateError):
        post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    with pytest.raises(CorruptStateError):
        user_prompt({"prompt": "next"}, store)

    # The CLI wrappers are what the host actually runs, and they must not raise.
    for command in ("hook-post-tool", "hook-user-prompt"):
        args = build_parser().parse_args(["--cwd", str(tmp_path), command, "--platform", "claude"])
        assert args.func(args) == 0


def test_quota_ingestion_survives_a_damaged_session(tmp_path):
    """On Claude Code this runs as the statusLine; it must never fail."""
    store = store_with_session(tmp_path)
    truncate_session(store)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=20.0)],
        sampled_at=time(),
    ))
    assert store.load_snapshot().window("seven_day").remaining_percent == 80.0


def test_an_unreadable_snapshot_degrades_to_unknown_quota(tmp_path):
    """Unlike the session, a damaged snapshot has a safe reading: no reading at all."""
    store = store_with_session(tmp_path)
    store.snapshot_path.write_text("{not json", encoding="utf-8")
    assert store.load_snapshot() is None

    _, decision = _evaluate(store)
    assert decision.quota_known is False
    assert decision.state == LandingState.CONSERVE


def _evaluate(store: StateStore):
    from agent_landing.hooks import evaluate_current

    return evaluate_current(store)


def test_cancel_works_on_the_state_that_caused_the_denial(tmp_path, capsys):
    store = store_with_session(tmp_path)
    truncate_session(store)
    args = build_parser().parse_args(["--cwd", str(tmp_path), "cancel"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "cancelled"

    # Standing down means the gate has nothing to enforce, and the evidence is kept.
    assert store.load_session() is None
    assert pre_tool(WRITE, store) == {}
    assert (store.root / "session.corrupt.json").exists()


def test_start_replaces_a_damaged_session(tmp_path, capsys):
    store = store_with_session(tmp_path)
    truncate_session(store)
    args = build_parser().parse_args([
        "--cwd", str(tmp_path), "start", "--provider", "claude", "--window", "seven_day",
        "--leave", "70", "--remaining", "90", "--task", "fresh",
    ])
    assert args.func(args) == 0
    assert store.load_session().task == "fresh"


def test_other_commands_report_a_damaged_session_without_a_traceback(tmp_path, capsys, monkeypatch):
    store = store_with_session(tmp_path)
    truncate_session(store)
    monkeypatch.setattr(sys, "argv", ["landing", "--cwd", str(tmp_path), "status"])
    assert main() == 1
    message = capsys.readouterr().err
    assert "landing cancel" in message
    assert "Traceback" not in message


def test_the_session_file_is_replaced_atomically(tmp_path):
    """A partially written session is a session the gate can no longer read."""
    store = store_with_session(tmp_path)
    original = store.session_path.read_text(encoding="utf-8")

    session = store.load_session()
    session.task = "x" * 4096
    store.save_session(session)

    # No temporary file is left behind next to the real one.
    assert [p.name for p in store.root.glob("session.json.*")] == []
    assert store.load_session().task == "x" * 4096
    assert original != store.session_path.read_text(encoding="utf-8")


def test_the_lock_is_taken_and_released(tmp_path):
    store = StateStore(tmp_path)
    with store.lock() as acquired:
        assert acquired is True
        assert store.lock_path.exists()
    assert not store.lock_path.exists()


def test_a_held_lock_degrades_instead_of_stalling_a_tool_call(tmp_path):
    """Waiting forever on a lock would be its own outage; a lost update is not."""
    store = StateStore(tmp_path)
    other = StateStore(tmp_path)
    with store.lock():
        started = time()
        with other.lock(timeout=0.1) as acquired:
            assert acquired is False
        assert time() - started < 2.0


def test_concurrent_tool_calls_do_not_lose_burn_accounting(tmp_path):
    """Hosts issue tool calls in parallel, and each one runs the gate separately.

    Unserialized, the calls read-modify-write the same session and overwrite each
    other: measured at 1 of 40 calls counted. An undercount biases the landing
    reserve *downwards*, which is the unsafe direction.
    """
    import threading

    store = store_with_session(tmp_path)
    payload = {"tool_name": "Read", "tool_input": {"file_path": "a"}}
    threads = [
        threading.Thread(target=pre_tool, args=(payload, StateStore(tmp_path)))
        for _ in range(40)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.load_session().tool_calls_since_snapshot == 40


def test_a_stale_lock_does_not_wedge_the_gate(tmp_path):
    """A hook killed mid-call must not leave every later tool call waiting."""
    import os

    store = StateStore(tmp_path)
    store.lock_path.write_text("", encoding="utf-8")
    ancient = time() - 3600
    os.utime(store.lock_path, (ancient, ancient))

    with store.lock(timeout=0.1) as acquired:
        assert acquired is True
