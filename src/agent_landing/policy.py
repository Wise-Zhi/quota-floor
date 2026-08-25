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
    """Estimate the quota needed to land, in percentage points.

    `burn_samples` are quota burned *per tool call*. Landing is priced in tool
    calls (inspect state, check git, write the handoff), so the samples and the
    multiplier share one unit. Sampling per snapshot interval instead would make
    the reserve drift with the host's snapshot refresh rate.
    """
    positive = [x for x in burn_samples[-12:] if x > 0]
    if len(positive) < 2:
        return config.default_reserve_percent

    # p90 protects against recent heavy calls; the median prevents one noisy sample
    # from collapsing the estimate when utilization reporting is coarse.
    p90 = percentile(positive, 0.90)
    typical = median(positive)
    per_tool_call = max(p90, typical)
    estimate = per_tool_call * config.landing_tool_calls * config.safety_factor
    return max(config.min_reserve_percent, min(config.max_reserve_percent, estimate))


#: NORMAL < CONSERVE < LAND < EMERGENCY. Used to combine independent signals by
#: taking the strictest, never the most permissive.
_SEVERITY = {
    LandingState.NORMAL: 0,
    LandingState.CONSERVE: 1,
    LandingState.LAND: 2,
    LandingState.EMERGENCY: 3,
}


def landing_feasibility(
    window_id: str,
    remaining_percent: float,
    reserve_percent: float,
    config: LandingConfig,
) -> tuple[LandingState, str]:
    """Can the agent still land, judged on one window?

    A landing reserve held on the budget window is worthless if a *different*
    window cuts the agent off first: once any window is exhausted the handoff
    cannot be written, no matter how healthy the budget looks.
    """
    if remaining_percent <= config.emergency_margin_percent:
        return LandingState.EMERGENCY, (
            f"The {window_id} quota is nearly exhausted ({remaining_percent:.1f}% left); "
            "preserve state immediately."
        )
    if remaining_percent <= reserve_percent:
        return LandingState.LAND, (
            f"The {window_id} quota ({remaining_percent:.1f}% left) no longer covers the landing "
            f"reserve ({reserve_percent:.1f}%); land now while a handoff is still possible."
        )
    if remaining_percent <= reserve_percent + config.conserve_margin_percent:
        return LandingState.CONSERVE, (
            f"The {window_id} quota ({remaining_percent:.1f}% left) is approaching the landing "
            "reserve; do not open large new workstreams."
        )
    return LandingState.NORMAL, ""


def unknown_quota_decision(session: TaskSession) -> PolicyDecision:
    """Decision used when no quota reading can be trusted for this session.

    Unknown is not 100%. A budget exists, so the guard must not fall silent just
    because the host stopped reporting the window it was pinned to.
    """
    cfg = session.config
    reserve = estimate_landing_reserve(session.burn_samples, cfg)
    last = session.last_remaining_percent
    return PolicyDecision(
        state=session.state if session.state == LandingState.STOPPED else LandingState.CONSERVE,
        remaining_percent=last if last is not None else session.started_remaining_percent,
        floor_percent=session.floor_percent,
        reserve_percent=reserve,
        development_floor_percent=min(100.0, session.floor_percent + reserve),
        reason=(
            f"No usable quota reading for the {session.window} window; unknown quota is not treated "
            "as available headroom. Do not open large new workstreams until it is readable again."
        ),
        quota_known=False,
    )


def evaluate(
    session: TaskSession,
    remaining_percent: float,
    snapshot_age_seconds: float | None = None,
    other_windows: list[tuple[str, float]] | None = None,
) -> PolicyDecision:
    cfg = session.config
    reserve = estimate_landing_reserve(session.burn_samples, cfg)
    development_floor = min(100.0, session.floor_percent + reserve)
    stale = snapshot_age_seconds is not None and snapshot_age_seconds > cfg.max_snapshot_age_seconds

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

    limiting_window: str | None = None
    if session.state != LandingState.STOPPED:
        # The floor belongs to the budget window, but landing must remain possible
        # on every window. Take the strictest signal, never the most permissive.
        for window_id, window_remaining in other_windows or []:
            other_state, other_reason = landing_feasibility(
                window_id, window_remaining, reserve, cfg
            )
            if _SEVERITY[other_state] > _SEVERITY[state]:
                state, reason, limiting_window = other_state, other_reason, window_id

    if stale and state == LandingState.NORMAL:
        # Unknown is not 100%. A snapshot this old is evidence of nothing, so it
        # must not authorize a new large workstream -- but it is also not proof
        # that quota is gone, so it does not force landing either.
        state = LandingState.CONSERVE
        reason = (
            f"Quota snapshot is {snapshot_age_seconds / 60:.0f} minutes old and cannot be trusted as "
            "current headroom; do not open large new workstreams until it refreshes."
        )

    return PolicyDecision(
        state=state,
        remaining_percent=remaining_percent,
        floor_percent=session.floor_percent,
        reserve_percent=reserve,
        development_floor_percent=development_floor,
        reason=reason,
        snapshot_age_seconds=snapshot_age_seconds,
        stale=stale,
        limiting_window=limiting_window,
    )
