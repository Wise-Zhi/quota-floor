from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median

from .models import PolicyDecision, QuotaSnapshot, TaskSession
from .policy import percentile


@dataclass
class RunwayEstimate:
    available: bool
    confidence: str
    method: str
    sample_count: int
    safe_headroom_percent: float
    recent_burn_percent_per_hour: float | None = None
    typical_burn_percent_per_hour: float | None = None
    heavy_burn_percent_per_hour: float | None = None
    recent_runway_hours: float | None = None
    typical_runway_hours: float | None = None
    heavy_runway_hours: float | None = None
    trend: str = "unknown"

    def to_dict(self):
        return asdict(self)


def _window_remaining(snapshot: QuotaSnapshot, session: TaskSession) -> float | None:
    if snapshot.provider != session.provider:
        return None
    window = snapshot.window(session.window)
    return None if window is None else window.remaining_percent


def _segment_rates(session: TaskSession, snapshots: list[QuotaSnapshot]) -> list[float]:
    points: list[tuple[float, float]] = []
    for snap in sorted(snapshots, key=lambda s: s.sampled_at):
        if snap.sampled_at + 1e-6 < session.started_at:
            continue
        remaining = _window_remaining(snap, session)
        if remaining is not None:
            points.append((snap.sampled_at, remaining))

    rates: list[float] = []
    for (t0, r0), (t1, r1) in zip(points, points[1:]):
        dt = t1 - t0
        drop = r0 - r1
        # Ignore duplicate timestamps, resets/increases, microscopic reporting noise,
        # and very long idle gaps that would make a wall-clock rate misleading.
        if dt < 1.0 or dt > 6 * 3600:
            continue
        if drop < 0.01 or drop > 25.0:
            continue
        rate = drop / (dt / 3600.0)
        # Extremely short reporting bursts can imply absurd instantaneous rates.
        # Clamp only for telemetry presentation; safety policy does not use this value.
        if 0 < rate <= 300.0:
            rates.append(rate)
    return rates[-24:]


def _hours(headroom: float, rate: float | None) -> float | None:
    if rate is None or rate <= 0:
        return None
    return max(0.0, headroom) / rate


def _confidence(n: int) -> str:
    if n < 2:
        return "low"
    if n < 6:
        return "medium"
    return "high"


def _trend(rates: list[float]) -> str:
    if len(rates) < 6:
        return "unknown"
    previous = median(rates[-6:-3])
    recent = median(rates[-3:])
    if previous <= 0:
        return "unknown"
    ratio = recent / previous
    if ratio >= 1.25:
        return "accelerating"
    if ratio <= 0.80:
        return "cooling"
    return "stable"


def estimate_runway(
    session: TaskSession,
    decision: PolicyDecision,
    snapshots: list[QuotaSnapshot],
) -> RunwayEstimate:
    """Estimate *runway from observed quota burn*, never total task cost.

    This is telemetry for the user/UI only. It is deliberately excluded from
    the quota enforcement path: the hard floor and landing reserve are based on
    actual quota snapshots and observed per-step burn, not on this estimate.
    """
    headroom = max(0.0, decision.remaining_percent - decision.development_floor_percent)
    rates = _segment_rates(session, snapshots)
    if not rates:
        return RunwayEstimate(
            available=False,
            confidence="low",
            method="waiting_for_observed_burn",
            sample_count=0,
            safe_headroom_percent=round(headroom, 4),
        )

    recent_rates = rates[-5:]
    recent = median(recent_rates)
    typical = median(rates)
    heavy = percentile(rates, 0.90)
    return RunwayEstimate(
        available=True,
        confidence=_confidence(len(rates)),
        method="observed_wall_clock_quota_burn",
        sample_count=len(rates),
        safe_headroom_percent=round(headroom, 4),
        recent_burn_percent_per_hour=round(recent, 4),
        typical_burn_percent_per_hour=round(typical, 4),
        heavy_burn_percent_per_hour=round(heavy, 4),
        recent_runway_hours=_round_hours(_hours(headroom, recent)),
        typical_runway_hours=_round_hours(_hours(headroom, typical)),
        heavy_runway_hours=_round_hours(_hours(headroom, heavy)),
        trend=_trend(rates),
    )


def _round_hours(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
