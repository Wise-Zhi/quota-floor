"""Ending the budget contract, as opposed to ending one task."""

from pathlib import Path
from time import time

import pytest

from agent_landing.hooks import evaluate_current, pre_tool, user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/x.py"}}


def store_with(tmp_path: Path, remaining: float = 91.0) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="task A", provider="claude", window="seven_day",
        floor_percent=70, started_remaining_percent=95, cwd=str(tmp_path),
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    return store


def cancel(store: StateStore) -> None:
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "cancelled"
    store.save_session(session)


def test_cancelling_stands_the_guard_down(tmp_path):
    store = store_with(tmp_path)
    cancel(store)
    assert pre_tool(WRITE, store) == {}, "a cancelled budget must not restrict anything"


def test_cancelled_budget_is_not_revived_by_a_new_user_prompt(tmp_path):
    """The whole point of cancelling: new instructions do not restart the floor."""
    store = store_with(tmp_path)
    cancel(store)
    assert user_prompt({"prompt": "something else entirely"}, store) == {}
    assert pre_tool(WRITE, store) == {}
    assert store.load_session().segment_count == 1


def test_next_user_prompt_releases_a_completed_guard(tmp_path):
    """Completion freezes the turn; the next real instruction clears the old task."""
    store = store_with(tmp_path)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    store.save_session(session)

    assert pre_tool(WRITE, store)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert user_prompt({"prompt": "next thing"}, store) == {}
    assert store.load_session().segment_count == 1
    assert store.load_session().stop_reason == "completed_released"
    assert pre_tool(WRITE, store) == {}

    cancel(store)
    assert pre_tool(WRITE, store) == {}, "cancel ends it"


def test_cancelled_session_is_kept_on_disk_not_deleted(tmp_path):
    """Burn samples and history stay useful for the next budget."""
    store = store_with(tmp_path)
    store_session = store.load_session()
    store_session.burn_samples = [0.5, 0.7]
    store.save_session(store_session)
    cancel(store)

    session = store.load_session()
    assert session is not None
    assert session.is_cancelled
    assert session.burn_samples == [0.5, 0.7]


def test_evaluate_current_reports_nothing_for_a_cancelled_budget(tmp_path):
    store = store_with(tmp_path)
    cancel(store)
    assert evaluate_current(store) is None


@pytest.mark.parametrize("reason, guarded", [
    ("task_complete", True),
    ("completed_released", False),
    ("landed", True),
    ("emergency", True),
    ("cancelled", False),
])
def test_only_cancellation_stands_the_guard_down(tmp_path, reason, guarded):
    store = store_with(tmp_path)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = reason
    store.save_session(session)
    denied = pre_tool(WRITE, store).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    assert denied is guarded
