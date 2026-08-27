"""The budget contract outlives a single task, without weakening the floor."""

from pathlib import Path
from time import time

from agent_landing.hooks import post_tool, pre_tool, user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore


def store_with_session(tmp_path: Path, remaining: float) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="task A",
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


def complete(store: StateStore) -> None:
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    session.completed_at_remaining_percent = 91.0
    store.save_session(session)


WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/next.py"}}


def test_agent_cannot_self_renew_after_completing(tmp_path):
    """The completion invariant: a tool call is not a new instruction."""
    store = store_with_session(tmp_path, 91.0)
    complete(store)
    result = pre_tool(WRITE, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.load_session().state == LandingState.STOPPED


def test_new_user_instruction_starts_a_new_segment_under_the_same_floor(tmp_path):
    store = store_with_session(tmp_path, 91.0)
    complete(store)

    out = user_prompt({"prompt": "now update the README"}, store)
    assert "new segment" in out["hookSpecificOutput"]["additionalContext"]

    session = store.load_session()
    assert session.state == LandingState.NORMAL
    assert session.segment_count == 2
    assert session.floor_percent == 70, "renewal must never move the user's floor"
    assert session.previous_tasks == ["task A"]

    assert pre_tool(WRITE, store).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_renewal_re_evaluates_quota_rather_than_assuming_headroom(tmp_path):
    """Renewing after completion must not hand back a budget that is already spent."""
    store = store_with_session(tmp_path, 91.0)
    complete(store)
    # Quota collapsed to the landing boundary while the session sat completed.
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - 73.5)],
        sampled_at=time(),
    ))
    user_prompt({"prompt": "one more thing"}, store)
    assert pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_landed_session_is_not_renewable_by_a_user_prompt(tmp_path):
    """A landed budget means quota actually ran out; a new prompt must not reopen it."""
    store = store_with_session(tmp_path, 73.5)
    pre_tool(WRITE, store)
    (tmp_path / "AGENT_HANDOFF.md").write_text("# handoff", encoding="utf-8")
    post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    assert store.load_session().stop_reason == "landed"

    assert user_prompt({"prompt": "keep going"}, store) == {}
    session = store.load_session()
    assert session.state == LandingState.STOPPED
    assert session.segment_count == 1
    assert pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_emergency_session_is_not_renewable_by_a_user_prompt(tmp_path):
    store = store_with_session(tmp_path, 70.4)
    pre_tool(WRITE, store)
    assert store.load_session().stop_reason == "emergency"

    assert user_prompt({"prompt": "keep going"}, store) == {}
    assert pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_user_prompt_is_a_noop_on_a_healthy_session(tmp_path):
    store = store_with_session(tmp_path, 91.0)
    assert user_prompt({"prompt": "hello"}, store) == {}
    session = store.load_session()
    assert session.segment_count == 1
    assert session.state == LandingState.NORMAL
