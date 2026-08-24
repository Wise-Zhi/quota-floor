from agent_landing.cli import _compact_status_line
from agent_landing.models import LandingState, PolicyDecision, TaskSession
from agent_landing.runway import RunwayEstimate


def test_compact_status_is_discoverable_and_short():
    session = TaskSession(
        task="demo",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=95,
    )
    decision = PolicyDecision(
        state=LandingState.NORMAL,
        remaining_percent=84,
        floor_percent=70,
        reserve_percent=3,
        development_floor_percent=73,
        reason="safe",
    )
    runway = RunwayEstimate(available=False, confidence="low", method="waiting_for_observed_burn", sample_count=0, safe_headroom_percent=11.0, trend="unknown")
    line = _compact_status_line(session, decision, runway)
    assert line == "AL | W 84.0->70.0% | safe +11.0% | NORMAL | runway learning"
