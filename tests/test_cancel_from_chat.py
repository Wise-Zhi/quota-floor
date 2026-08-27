"""Ending the contract from the chat, without a terminal.

The gate denies `landing cancel` as a tool call on purpose: an agent must never
lift its own quota floor. But that left the contract with no exit at all for a
user who is away from a shell -- the guard blocks their agent and the only
sanctioned release is a command they cannot reach.

`UserPromptSubmit` is the one channel an agent cannot manufacture, and it already
receives the user's own words. So it cancels directly. The rule that keeps this
safe is: recognize loosely, act strictly.
"""

from pathlib import Path
from time import time

from agent_landing.hooks import (
    is_cancel_invocation,
    pre_tool,
    suggests_cancel,
    user_prompt,
)
from agent_landing.invocation import parse_invocation
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/next.py"}}


def store_in_land(tmp_path: Path, remaining: float = 73.5) -> StateStore:
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


def test_the_gate_blocks_the_agent_from_cancelling(tmp_path):
    """The premise of the whole feature: this must stay denied."""
    store = store_in_land(tmp_path)
    denied = pre_tool(
        {"tool_name": "Bash", "tool_input": {"command": "landing cancel"}}, store
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_denial_names_the_way_out(tmp_path):
    """A gate with no reachable exit is a trap, so the exit rides on the denial."""
    store = store_in_land(tmp_path)
    reason = pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "/landing cancel" in reason


def test_the_explicit_command_cancels_from_the_prompt(tmp_path):
    store = store_in_land(tmp_path)
    result = user_prompt({"prompt": "/landing cancel"}, store)

    assert "cancelled" in result["hookSpecificOutput"]["additionalContext"]
    assert store.load_session().is_cancelled
    # Standing down means the gate has nothing left to enforce.
    assert pre_tool(WRITE, store) == {}


def test_both_host_spellings_work(tmp_path):
    for spelling in ("/landing cancel", "$landing cancel", "landing cancel", "  /LANDING   cancel  "):
        assert is_cancel_invocation(spelling), spelling


def test_a_mention_of_the_command_is_not_the_command(tmp_path):
    """The single most important case: discussing it must not trigger it.

    Substring matching would make "why does /landing cancel not work?" silently
    unguard the user, which is the failure mode this project ranks below having
    no guard at all.
    """
    store = store_in_land(tmp_path)
    for text in (
        "why does /landing cancel not work?",
        "explain what /landing cancel does",
        "add a test for /landing cancel",
    ):
        assert not is_cancel_invocation(text), text
        user_prompt({"prompt": text}, store)
        assert not store.load_session().is_cancelled, text


def test_natural_language_hints_but_never_acts(tmp_path):
    """A guess that unguards the user is worse than no guess at all."""
    store = store_in_land(tmp_path)
    before = store.session_path.read_text(encoding="utf-8")

    result = user_prompt({"prompt": "取消预算"}, store)

    assert "/landing cancel" in result["hookSpecificOutput"]["additionalContext"]
    assert not store.load_session().is_cancelled
    assert store.session_path.read_text(encoding="utf-8") == before, "state must not move"
    assert pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hint_recognizes_both_languages():
    for text in ("please cancel the budget", "stop watching my quota", "取消预算", "别管配额了"):
        assert suggests_cancel(text), text
    for text in ("cancel the deployment", "stop the server", "leave 70 -- do the thing"):
        assert not suggests_cancel(text), text


def test_a_landed_budget_can_still_be_stood_down(tmp_path):
    """`landed` still gates every tool call, so it needs an exit too."""
    store = store_in_land(tmp_path)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "landed"
    store.save_session(session)

    user_prompt({"prompt": "/landing cancel"}, store)
    assert store.load_session().is_cancelled
    assert pre_tool(WRITE, store) == {}


def test_cancelling_twice_is_a_no_op(tmp_path):
    store = store_in_land(tmp_path)
    user_prompt({"prompt": "/landing cancel"}, store)
    result = user_prompt({"prompt": "/landing cancel"}, store)

    assert "already cancelled" in result["hookSpecificOutput"]["additionalContext"]
    assert store.load_session().is_cancelled


def test_cancelling_with_no_budget_says_so(tmp_path):
    store = StateStore(tmp_path)
    result = user_prompt({"prompt": "/landing cancel"}, store)
    assert "no active quota budget" in result["hookSpecificOutput"]["additionalContext"]


def test_cancelling_does_not_renew_a_completed_segment(tmp_path):
    """Cancel and renew both live in this hook; they must not run together."""
    store = store_in_land(tmp_path, remaining=91.0)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    store.save_session(session)

    user_prompt({"prompt": "/landing cancel"}, store)
    session = store.load_session()
    assert session.is_cancelled
    assert session.segment_count == 1, "cancelling is not the start of a new segment"


def test_the_parser_understands_the_bare_verb():
    for text in ("cancel", "stop", "abort", "CANCEL"):
        parsed = parse_invocation(text)
        assert parsed.ok and parsed.action == "cancel", text
        assert parsed.to_dict() == {"ok": True, "action": "cancel"}


def test_the_verb_inside_a_task_is_still_just_a_task():
    """Ending a contract is not something to infer from a passing mention."""
    parsed = parse_invocation("leave 70 -- cancel the stale deployment job")
    assert parsed.ok and parsed.action == "start"
    assert parsed.task == "cancel the stale deployment job"
