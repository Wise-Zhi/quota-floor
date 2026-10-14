from pathlib import Path

from agent_landing.hooks import pre_tool
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore


def setup_store(tmp_path: Path, remaining: float) -> StateStore:
    store = StateStore(tmp_path)
    s = TaskSession(
        task="implement auth",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=90,
        cwd=str(tmp_path),
    )
    store.save_session(s)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=1,
    ))
    return store


def test_task_complete_stops_even_far_above_floor(tmp_path):
    store = setup_store(tmp_path, 91.0)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    session.completed_at_remaining_percent = 91.0
    store.save_session(session)
    result = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/waste.py"}}, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "stopped" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()
