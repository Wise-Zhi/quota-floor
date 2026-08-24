from agent_landing.models import LandingConfig, LandingState, TaskSession
from agent_landing.policy import estimate_landing_reserve, evaluate


def session(**kwargs):
    base = dict(
        task="x",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=100,
    )
    base.update(kwargs)
    return TaskSession(**base)


def test_default_reserve_before_history():
    s = session()
    assert estimate_landing_reserve([], s.config) == 4.0
    assert evaluate(s, 80).state == LandingState.NORMAL
    assert evaluate(s, 77).state == LandingState.CONSERVE
    assert evaluate(s, 74).state == LandingState.LAND


def test_dynamic_reserve_grows_with_burn_rate():
    s = session(burn_samples=[1.0, 1.4, 1.2, 1.6])
    reserve = estimate_landing_reserve(s.burn_samples, s.config)
    assert reserve > 4.0
    decision = evaluate(s, 77)
    assert decision.development_floor_percent > 74


def test_emergency_near_user_floor():
    s = session()
    assert evaluate(s, 70.5).state == LandingState.EMERGENCY
