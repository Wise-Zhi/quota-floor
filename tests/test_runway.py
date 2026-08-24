from agent_landing.models import PolicyDecision, LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.runway import estimate_runway


def snap(t: float, remaining: float) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=t,
    )


def session() -> TaskSession:
    return TaskSession(
        task="x",
        provider="claude",
        window="seven_day",
        floor_percent=75,
        started_remaining_percent=95,
        started_at=0,
    )


def decision(remaining: float = 90) -> PolicyDecision:
    return PolicyDecision(
        state=LandingState.NORMAL,
        remaining_percent=remaining,
        floor_percent=75,
        reserve_percent=3,
        development_floor_percent=78,
        reason="test",
    )


def test_runway_waits_for_real_quota_movement():
    r = estimate_runway(session(), decision(), [snap(0, 95)])
    assert not r.available
    assert r.method == "waiting_for_observed_burn"


def test_runway_uses_observed_burn_speed_not_task_progress():
    # 4 percentage points over one hour => 4%/h. With 12% safe headroom => ~3h.
    r = estimate_runway(session(), decision(90), [snap(0, 94), snap(3600, 90)])
    assert r.available
    assert r.recent_burn_percent_per_hour == 4.0
    assert r.safe_headroom_percent == 12.0
    assert r.recent_runway_hours == 3.0


def test_runway_ignores_quota_resets():
    r = estimate_runway(session(), decision(90), [snap(0, 90), snap(3600, 95), snap(7200, 91)])
    assert r.available
    assert r.sample_count == 1
    assert r.recent_burn_percent_per_hour == 4.0
