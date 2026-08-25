"""Plans differ in which quota windows exist, and any window can block landing.

A plan can expose several windows at once: a Codex Plus account reports both a
five-hour and a weekly one. The floor is a limit on *every* window, not only on
the one the budget was pinned to, and whichever window reaches it first is the
one that stops the work.

Bands used below, for floor=70 with the default reserve of 4.0:
NORMAL > 79.0, CONSERVE (74.0, 79.0], LAND (70.75, 74.0], EMERGENCY <= 70.75.
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
    """Inside the reserve above its floor but not yet at it: land now."""
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 72.0)])
    assert decision.state == LandingState.LAND
    assert decision.limiting_window == "five_hour"
    assert "five_hour" in decision.reason


def test_a_second_window_past_the_floor_is_an_emergency(tmp_path):
    """Far below the floor is not merely "time to land"; the reserve is gone."""
    session = store_with(tmp_path, "seven_day", snapshot(seven_day=91)).load_session()
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 2.0)])
    assert decision.state == LandingState.EMERGENCY
    assert decision.limiting_window == "five_hour"


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
    decision = evaluate(session, 91.0, other_windows=[("five_hour", 76.0)])
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


# --- the floor is a limit on every window -------------------------------------

def codex_store(tmp_path: Path, **kwargs) -> StateStore:
    """A Codex Plus session: both windows present, floor on `auto`."""
    store = StateStore(tmp_path)
    kwargs.setdefault("floor_percent", 70)
    session = TaskSession(
        task="t", provider="codex", window="seven_day",
        started_remaining_percent=94, cwd=str(tmp_path), **kwargs,
    )
    session.started_remaining_by_window = {"five_hour": 94.0, "seven_day": 99.0}
    store.save_session(session)
    return store


def codex_snapshot(five_hour: float, seven_day: float) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="codex",
        windows=[
            QuotaWindow(id="five_hour", used_percent=100 - five_hour),
            QuotaWindow(id="seven_day", used_percent=100 - seven_day),
        ],
        sampled_at=time(),
    )


def test_the_floor_binds_on_whichever_window_reaches_it_first(tmp_path):
    """`leave 70` on a Plus account must bind on the five-hour window.

    Regression for the live defect found against a real payload: because Plus
    reports both windows, `auto` pinned the floor to the weekly one, and the
    five-hour window burned from 94% to 3% remaining while the state stayed
    NORMAL. The floor never bound; only the landing reserve eventually did.
    """
    store = codex_store(tmp_path)
    store.save_snapshot(codex_snapshot(five_hour=71.0, seven_day=96.0))
    _, decision = evaluate_current(store)
    assert decision.state != LandingState.NORMAL
    assert decision.limiting_window == "five_hour"


def test_a_roomy_weekly_window_does_not_buy_headroom_on_the_five_hour_one(tmp_path):
    store = codex_store(tmp_path)
    store.save_snapshot(codex_snapshot(five_hour=30.0, seven_day=90.0))
    _, decision = evaluate_current(store)
    assert decision.state == LandingState.EMERGENCY
    assert decision.limiting_window == "five_hour"


def test_both_windows_above_the_floor_still_work_normally(tmp_path):
    """The floor is a limit, not a brake: healthy quota must stay unobstructed."""
    store = codex_store(tmp_path)
    store.save_snapshot(codex_snapshot(five_hour=94.0, seven_day=99.0))
    _, decision = evaluate_current(store)
    assert decision.state == LandingState.NORMAL
    assert decision.limiting_window is None


def test_spend_is_measured_from_each_window_own_starting_point(tmp_path):
    """`spend 15` means 15 points off *that* window, not off the pinned one.

    The windows burn at very different rates, so measuring every window against
    the budget window's starting point makes the delta meaningless. five_hour
    started at 94 -> its floor is 79 (NORMAL above 88); seven_day started at 99
    -> its floor is 84 (NORMAL above 93). At 90% remaining five_hour is fine on
    its own baseline, but would read as CONSERVE against seven_day's.
    """
    store = codex_store(tmp_path, max_spend_percent=15.0, floor_percent=84.0)
    store.save_snapshot(codex_snapshot(five_hour=90.0, seven_day=99.0))
    _, decision = evaluate_current(store)
    assert decision.state == LandingState.NORMAL
    assert decision.limiting_window is None


# --- an unreadable window is not an absent one --------------------------------

def test_an_unreadable_second_window_forces_conserve(tmp_path):
    store = codex_store(tmp_path)
    snap = codex_snapshot(five_hour=94.0, seven_day=99.0)
    snap.windows = [w for w in snap.windows if w.id != "five_hour"]
    snap.unreadable_windows = [{"id": "five_hour", "reason": "no recognized field"}]
    store.save_snapshot(snap)
    _, decision = evaluate_current(store)
    assert decision.state == LandingState.CONSERVE
    assert decision.limiting_window == "five_hour"


def test_an_unreadable_budget_window_does_not_migrate_to_a_roomier_one(tmp_path):
    """Present but unparseable must not be mistaken for "this plan lost a window".

    Migrating here moved the budget onto the weekly window -- almost always the
    more permissive one -- and reported the result as a known quota.
    """
    store = codex_store(tmp_path)
    session = store.load_session()
    session.window = "five_hour"
    store.save_session(session)
    snap = codex_snapshot(five_hour=94.0, seven_day=99.0)
    snap.windows = [w for w in snap.windows if w.id != "five_hour"]
    snap.unreadable_windows = [{"id": "five_hour", "reason": "no recognized field"}]
    store.save_snapshot(snap)

    session, decision = evaluate_current(store)
    assert session.window == "five_hour"
    assert decision.quota_known is False
    assert decision.state == LandingState.CONSERVE
