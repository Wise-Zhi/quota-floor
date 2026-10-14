"""`spend N` is measured on the five-hour window when the plan has one.

Most users are on Plus, where the five-hour window is what actually limits a
session; only Pro-style plans have no five-hour limit. Weekly percentage points
are too coarse a unit for "spend at most N on this task" (the user, 2026-10-15).
So `spend` without an explicit window binds to `five_hour`, falling back to the
longest window when there is none. The other windows keep only the
landing-feasibility check: they still force a landing if they would otherwise
run out before a handoff could be written.

`leave N` is unchanged: it names an absolute level and guards every window.
"""

from time import time

from agent_landing.budget import start_budget
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow
from agent_landing.policy import evaluate, window_floor
from agent_landing.state import StateStore


def snap(five_hour=None, seven_day=None) -> QuotaSnapshot:
    windows = []
    if five_hour is not None:
        windows.append(QuotaWindow(id="five_hour", used_percent=100 - five_hour))
    if seven_day is not None:
        windows.append(QuotaWindow(id="seven_day", used_percent=100 - seven_day))
    return QuotaSnapshot(provider="codex", windows=windows, sampled_at=time())


def open_spend(tmp_path, snapshot, spend=10.0, window="auto"):
    store = StateStore(tmp_path)
    start_budget(store, snapshot, provider="codex", task="t", window=window, max_spend=spend,
                 authorized_by_user=True)
    return store.load_session()


def test_spend_binds_to_the_five_hour_window_on_a_plus_plan(tmp_path):
    session = open_spend(tmp_path, snap(five_hour=63, seven_day=31), spend=10)
    assert session.window == "five_hour"
    assert session.floor_percent == 53.0
    assert window_floor(session, "five_hour") == 53.0


def test_the_weekly_window_keeps_only_the_feasibility_check(tmp_path):
    session = open_spend(tmp_path, snap(five_hour=63, seven_day=31), spend=10)
    assert window_floor(session, "seven_day") == 0.0

    # Weekly falling 10 points is not this budget's concern ...
    calm = evaluate(session, 63.0, other_windows=[("seven_day", 21.0)])
    assert calm.state == LandingState.NORMAL
    # ... but weekly running out still forces a landing.
    nearly_out = evaluate(session, 60.0, other_windows=[("seven_day", 3.0)])
    assert nearly_out.state in (LandingState.LAND, LandingState.EMERGENCY)


def test_spending_the_allowance_on_five_hour_lands(tmp_path):
    session = open_spend(tmp_path, snap(five_hour=63, seven_day=31), spend=10)
    assert evaluate(session, 55.0, other_windows=[("seven_day", 30.0)]).state == LandingState.LAND


def test_a_plan_without_a_five_hour_window_spends_against_the_longest(tmp_path):
    session = open_spend(tmp_path, snap(seven_day=80), spend=10)
    assert session.window == "seven_day"
    assert session.floor_percent == 70.0


def test_an_explicit_window_still_wins(tmp_path):
    session = open_spend(tmp_path, snap(five_hour=63, seven_day=31), spend=5, window="seven_day")
    assert session.window == "seven_day"
    assert session.floor_percent == 26.0


def test_leave_still_guards_every_window(tmp_path):
    store = StateStore(tmp_path)
    start_budget(store, snap(five_hour=90, seven_day=80), provider="codex", task="t", preserve=50,
                 authorized_by_user=True)
    session = store.load_session()
    assert session.floor_applies_to_all_windows is True
    assert window_floor(session, "five_hour") == 50.0
    assert window_floor(session, "seven_day") == 50.0
