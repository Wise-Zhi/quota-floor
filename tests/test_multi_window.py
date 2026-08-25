"""Plans differ in which quota windows exist, and any window can block landing.

Pro accounts expose a weekly window; Plus accounts may only expose the five-hour
one. The floor belongs to the budget window (the longest available), but landing
must stay possible on *every* window -- whichever runs out first is the one that
makes the handoff impossible.
"""

from pathlib import Path
from time import time

from agent_landing.hooks import evaluate_current, pre_tool, resolve_budget_window
from agent_landing.models import (
    LandingState,
    QuotaSnapshot,
    QuotaWindow,
    TaskSession,
    window_minutes,
)
from agent_landing.policy import evaluate
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/x.py"}}


def snapshot(**windows: float) -> QuotaSnapshot:
    """Build a snapshot from window_id=remaining_percent pairs."""
    return QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id=wid, used_percent=100 - rem) for wid, rem in windows.items()],
        sampled_at=time(),
    )


def store_with(tmp_path: Path, window: str, snap: QuotaSnapshot) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="t",
        provider="claude",
        window=window,
        floor_percent=70,
        started_remaining_percent=95,
        cwd=str(tmp_path),
    ))
    store.save_snapshot(snap)
    return store


# --- picking the budget window ------------------------------------------------

def test_window_durations_rank_weekly_above_five_hour():
    assert window_minutes("seven_day") > window_minutes("five_hour")
    assert window_minutes("window_4320m") == 4320
    assert window_minutes("mystery") == 0


def test_pro_account_budget_window_is_weekly():
    assert snapshot(five_hour=90, seven_day=80).longest_window().id == "seven_day"


def test_plus_account_budget_window_is_the_five_hour_one():
    """With no weekly window, the five-hour window *is* the budget."""
    assert snapshot(five_hour=90).longest_window().id == "five_hour"


def test_no_windows_yields_no_budget_window():
    assert snapshot().longest_window() is None


# --- a vanished window must never mean "no guard" -----------------------------

def test_budget_migrates_when_the_plan_loses_its_weekly_window(tmp_path):
    store = store_with(tmp_path, "seven_day", snapshot(five_hour=90))
    session = store.load_session()
    window = resolve_budget_window(session, snapshot(five_hour=90))
    assert window.id == "five_hour"
    assert session.window == "five_hour", "the migration must be persisted"


def test_vanished_window_does_not_silently_stop_guarding(tmp_path):
    """The old behaviour returned no decision at all, which allowed everything."""
    store = store_with(tmp_path, "seven_day", snapshot(five_hour=5))
    result = pre_tool(WRITE, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_readable_window_is_treated_as_unknown_not_as_full(tmp_path):
    store = store_with(tmp_path, "seven_day", snapshot())
    session, decision = evaluate_current(store)
    assert decision.quota_known is False
    assert decision.state == LandingState.CONSERVE
    assert "not treated as available headroom" in decision.reason


# --- landing feasibility is checked on every window ---------------------------

def test_healthy_budget_window_cannot_mask_an_exhausted_rate_limit(tmp_path):
    """Weekly at 91% is irrelevant if the five-hour window is about to cut us off."""
    store = store_with(tmp_path, "seven_day", snapshot(five_hour=2, seven_day=91))
    result = pre_tool(WRITE, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_limiting_window_is_named_in_the_decision(tmp_path):
    """Below the landing reserve but above the emergency margin: land now."""
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 2.0)])
    assert decision.state == LandingState.LAND
    assert decision.limiting_window == "five_hour"
    assert "five_hour" in decision.reason


def test_second_window_inside_the_emergency_margin_forces_emergency(tmp_path):
    """Too little left for even a model-written handoff on that window."""
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 0.5)])
    assert decision.state == LandingState.EMERGENCY
    assert decision.limiting_window == "five_hour"


def test_a_comfortable_second_window_changes_nothing(tmp_path):
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 88.0)])
    assert decision.state == LandingState.NORMAL
    assert decision.limiting_window is None


def test_cross_window_check_only_tightens_never_relaxes(tmp_path):
    """A full five-hour window must not rescue a budget window at its floor."""
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=73.5)).load_session()
    decision = evaluate(session, 73.5, other_windows=[("five_hour", 100.0)])
    assert decision.state == LandingState.LAND


def test_second_window_near_reserve_triggers_conserve(tmp_path):
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 7.0)])
    assert decision.state == LandingState.CONSERVE
    assert decision.limiting_window == "five_hour"


def test_completed_session_is_not_reopened_by_another_window(tmp_path):
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 2.0)])
    assert decision.state == LandingState.STOPPED


def test_unknown_quota_is_never_reported_as_a_current_number(tmp_path):
    """The number shown must not read as a live measurement when it is not one."""
    store = store_with(tmp_path, "seven_day", snapshot())
    session, decision = evaluate_current(store)
    assert "current quota unreadable" in decision.describe_remaining()


def test_blind_statusline_does_not_leave_the_last_good_reading_in_force(tmp_path):
    """A host that stops reporting must not keep the guard judging an old number."""
    store = store_with(tmp_path, "seven_day", snapshot(seven_day=91))
    _, healthy = evaluate_current(store)
    assert healthy.quota_known and healthy.state == LandingState.NORMAL

    blind = snapshot()
    blind.unreadable_windows = [{"id": "seven_day", "reason": "no recognized field"}]
    store.save_snapshot(blind)

    _, decision = evaluate_current(store)
    assert decision.quota_known is False
    assert decision.state == LandingState.CONSERVE
