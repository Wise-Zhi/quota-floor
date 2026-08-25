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


def window_floor(session: TaskSession, window_id: str) -> float:
    """The floor as it applies to one window.

    `leave N` names an absolute level, so it reads identically on every window.
    `spend N` names a delta, so it has to be measured from where *that* window
    stood when the budget started -- the windows burn at very different rates,
    and 15 points off a weekly window is not 15 points off a five-hour one.
    """
    if window_id != session.window and not session.floor_applies_to_all_windows:
        # The user scoped the floor to one window. Other windows keep a floor of
        # zero, which reduces window_pressure to the pure landing-feasibility
        # check: the handoff must stay writable no matter which window runs out.
        return 0.0
    if session.max_spend_percent is None:
        return session.floor_percent
    start = session.started_remaining_by_window.get(window_id)
    if start is None:
        # A window that appeared after the budget started has no baseline to
        # measure a delta from. Fall back to the session floor rather than
        # inventing a fresh allowance for it.
        return session.floor_percent
    return max(0.0, start - session.max_spend_percent)


def window_pressure(
    window_id: str,
    remaining_percent: float,
    floor_percent: float,
    reserve_percent: float,
    config: LandingConfig,
) -> tuple[LandingState, str]:
    """The state one window forces, given the floor that applies to it.

    The floor is a limit on *every* window the plan exposes, not only on the one
    the budget was pinned to. A Plus account carries both a five-hour and a
    weekly window, and inside a single session the five-hour one is what
    actually binds -- pinning the floor to the longest window let the five-hour
    window burn from 94% to 3% while the state stayed NORMAL.

    The reserve sits on top of the floor so a handoff is still writable when the
    floor is reached; reaching the floor itself means the reserve is gone.
    """
    development_floor = min(100.0, floor_percent + reserve_percent)
    if remaining_percent <= config.emergency_margin_percent:
        return LandingState.EMERGENCY, (
            f"The {window_id} quota is nearly exhausted ({remaining_percent:.1f}% left); "
            "preserve state immediately."
        )
    if remaining_percent <= floor_percent + config.emergency_margin_percent:
        return LandingState.EMERGENCY, (
            f"The {window_id} quota ({remaining_percent:.1f}% left) has reached the floor "
            f"({floor_percent:.1f}%); preserve state immediately."
        )
    if remaining_percent <= development_floor:
        return LandingState.LAND, (
            f"The {window_id} quota ({remaining_percent:.1f}% left) has reached the landing "
            f"reserve above its floor ({development_floor:.1f}%); land now while a handoff "
            "is still possible."
        )
    if remaining_percent <= development_floor + config.conserve_margin_percent:
        return LandingState.CONSERVE, (
            f"The {window_id} quota ({remaining_percent:.1f}% left) is approaching its landing "
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


#: Wording for the budget window. Other windows describe themselves by name,
#: because "which window stopped me" is the first thing the agent needs to know.
_BUDGET_REASON = {
    LandingState.EMERGENCY: "Quota is inside the emergency margin; preserve state immediately.",
    LandingState.LAND: "Remaining quota has reached the landing reserve boundary.",
    LandingState.CONSERVE: (
        "Quota is approaching the landing reserve; do not open large new workstreams."
    ),
    LandingState.NORMAL: "There is sufficient quota headroom for normal work.",
}


def evaluate(
    session: TaskSession,
    remaining_percent: float,
    snapshot_age_seconds: float | None = None,
    other_windows: list[tuple[str, float]] | None = None,
    unreadable_windows: list[str] | None = None,
) -> PolicyDecision:
    cfg = session.config
    reserve = estimate_landing_reserve(session.burn_samples, cfg)
    budget_floor = window_floor(session, session.window)
    development_floor = min(100.0, budget_floor + reserve)
    stale = snapshot_age_seconds is not None and snapshot_age_seconds > cfg.max_snapshot_age_seconds

    if session.state == LandingState.STOPPED:
        state = LandingState.STOPPED
        if session.stop_reason == "task_complete":
            reason = "The task completed before the quota floor; no further work should be performed."
        else:
            reason = "The task has already landed and the handoff is complete."
    else:
        state, _ = window_pressure(
            session.window, remaining_percent, budget_floor, reserve, cfg
        )
        reason = _BUDGET_REASON[state]

    limiting_window: str | None = None
    if session.state != LandingState.STOPPED:
        # The floor is a limit on every window, so each one is judged against the
        # floor as it applies there and the strictest result wins. Never the most
        # permissive: a roomy weekly window is not evidence about the five-hour one.
        for window_id, window_remaining in other_windows or []:
            other_state, other_reason = window_pressure(
                window_id, window_remaining, window_floor(session, window_id), reserve, cfg
            )
            if _SEVERITY[other_state] > _SEVERITY[state]:
                state, reason, limiting_window = other_state, other_reason, window_id

        for window_id in unreadable_windows or []:
            # The floor cannot be checked on a window whose quota will not parse,
            # and an unreadable window is not an absent one. Refuse to treat it as
            # headroom without claiming it is exhausted either.
            if _SEVERITY[LandingState.CONSERVE] > _SEVERITY[state]:
                state = LandingState.CONSERVE
                limiting_window = window_id
                reason = (
                    f"The {window_id} quota cannot be read, so the floor cannot be checked "
                    "there; do not open large new workstreams until it is readable again."
                )

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
        floor_percent=budget_floor,
        reserve_percent=reserve,
        development_floor_percent=development_floor,
        reason=reason,
        snapshot_age_seconds=snapshot_age_seconds,
        stale=stale,
        limiting_window=limiting_window,
    )
