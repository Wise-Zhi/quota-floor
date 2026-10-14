"""A Landing budget belongs to one requested task."""

from pathlib import Path
from time import time

from agent_landing.hooks import pre_tool, stop, user_prompt
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


def test_completed_task_blocks_more_work_in_the_same_turn(tmp_path):
    """The agent cannot say complete and then continue spending in the same turn."""
    store = store_with_session(tmp_path, 91.0)
    complete(store)
    result = pre_tool(WRITE, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.load_session().state == LandingState.STOPPED


def test_new_user_instruction_does_not_restart_a_completed_budget(tmp_path):
    store = store_with_session(tmp_path, 91.0)
    complete(store)

    out = user_prompt({"prompt": "now update the README"}, store)
    assert out == {}

    session = store.load_session()
    assert session.state == LandingState.STOPPED
    assert session.stop_reason == "completed_released"
    assert session.segment_count == 1
    assert session.floor_percent == 70

    assert pre_tool(WRITE, store).get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_completed_budget_stays_inactive_even_if_quota_later_falls(tmp_path):
    store = store_with_session(tmp_path, 91.0)
    complete(store)
    # Quota collapsed to the landing boundary while the session sat completed.
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - 73.5)],
        sampled_at=time(),
    ))
    user_prompt({"prompt": "one more thing"}, store)
    assert pre_tool(WRITE, store) == {}


def test_landed_session_is_not_renewable_by_a_user_prompt(tmp_path):
    """A landed budget means quota actually ran out; a new prompt must not reopen it."""
    store = store_with_session(tmp_path, 73.5)
    pre_tool(WRITE, store)
    stop({"last_assistant_message": "## Next steps 1. finish"}, store)
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


def test_legacy_auto_renewed_segment_is_released_on_upgrade(tmp_path):
    """Old segment 2 state must not keep leaking a floor after this fix ships."""
    store = store_with_session(tmp_path, 91.0)
    session = store.load_session()
    session.segment_count = 2
    store.save_session(session)

    assert pre_tool(WRITE, store) == {}
