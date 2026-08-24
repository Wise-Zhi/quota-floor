from __future__ import annotations

import math
from statistics import median

from .models import LandingConfig, LandingState, PolicyDecision, TaskSession


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * q
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def estimate_landing_reserve(burn_samples: list[float], config: LandingConfig) -> float:
    positive = [x for x in burn_samples[-12:] if x > 0]
    if len(positive) < 2:
        return config.default_reserve_percent

    # p90 protects against recent heavy turns; the median prevents one noisy sample
    # from collapsing the estimate when utilization reporting is coarse.
    p90 = percentile(positive, 0.90)
    typical = median(positive)
    per_turn = max(p90, typical)
    estimate = per_turn * config.landing_turns * config.safety_factor
    return max(config.min_reserve_percent, min(config.max_reserve_percent, estimate))


def evaluate(session: TaskSession, remaining_percent: float) -> PolicyDecision:
    cfg = session.config
    reserve = estimate_landing_reserve(session.burn_samples, cfg)
    development_floor = min(100.0, session.floor_percent + reserve)

    if session.state == LandingState.STOPPED:
        state = LandingState.STOPPED
        if session.stop_reason == "task_complete":
            reason = "The task completed before the quota floor; no further work should be performed."
        else:
            reason = "The task has already landed and the handoff is complete."
    elif remaining_percent <= session.floor_percent + cfg.emergency_margin_percent:
        state = LandingState.EMERGENCY
        reason = "Quota is inside the emergency margin; preserve state immediately."
    elif remaining_percent <= development_floor:
        state = LandingState.LAND
        reason = "Remaining quota has reached the landing reserve boundary."
    elif remaining_percent <= development_floor + cfg.conserve_margin_percent:
        state = LandingState.CONSERVE
        reason = "Quota is approaching the landing reserve; do not open large new workstreams."
    else:
        state = LandingState.NORMAL
        reason = "There is sufficient quota headroom for normal work."

    return PolicyDecision(
        state=state,
        remaining_percent=remaining_percent,
        floor_percent=session.floor_percent,
        reserve_percent=reserve,
        development_floor_percent=development_floor,
        reason=reason,
    )
