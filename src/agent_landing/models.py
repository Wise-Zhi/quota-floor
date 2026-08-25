from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any
from time import time


class LandingState(str, Enum):
    NORMAL = "normal"
    CONSERVE = "conserve"
    LAND = "land"
    EMERGENCY = "emergency"
    STOPPED = "stopped"


@dataclass
class QuotaWindow:
    id: str
    used_percent: float
    resets_at: int | None = None
    source: str = "unknown"
    confidence: str = "high"

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaWindow":
        return cls(**data)


#: Known window durations, in minutes. Used to rank windows by how expensive they
#: are to exhaust: a 5-hour window is a rate limit that refills after a nap, while
#: a weekly window is the actual budget and costs days to get back.
WINDOW_MINUTES: dict[str, int] = {"five_hour": 300, "seven_day": 10080}

_GENERIC_WINDOW = re.compile(r"^window_(\d+)m$")


def window_minutes(window_id: str) -> int:
    """Duration of a window in minutes, or 0 when it cannot be determined."""
    if window_id in WINDOW_MINUTES:
        return WINDOW_MINUTES[window_id]
    match = _GENERIC_WINDOW.match(window_id)
    return int(match.group(1)) if match else 0


@dataclass
class QuotaSnapshot:
    provider: str
    windows: list[QuotaWindow]
    sampled_at: float
    #: Windows the provider reported but whose quota could not be trusted, as
    #: ``{"id": ..., "reason": ...}``. Recorded rather than dropped so the user is
    #: told a window is unguarded instead of it silently disappearing.
    unreadable_windows: list[dict[str, str]] = field(default_factory=list)

    def window(self, window_id: str) -> QuotaWindow | None:
        return next((w for w in self.windows if w.id == window_id), None)

    def longest_window(self) -> QuotaWindow | None:
        """The longest window still reported, used when a pinned one disappears.

        This is NOT where the floor lives. A Codex Plus account reports both a
        five-hour and a weekly window, so treating the longest one as "the
        user's budget" let the five-hour window -- the one that actually binds
        inside a session -- burn unguarded. The floor applies to every window;
        this only decides which window a homeless budget re-pins to.
        """
        if not self.windows:
            return None
        return max(self.windows, key=lambda w: (window_minutes(w.id), w.id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "windows": [asdict(w) | {"remaining_percent": w.remaining_percent} for w in self.windows],
            "sampled_at": self.sampled_at,
            "unreadable_windows": self.unreadable_windows,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaSnapshot":
        windows: list[QuotaWindow] = []
        for raw in data.get("windows", []):
            raw = dict(raw)
            raw.pop("remaining_percent", None)
            windows.append(QuotaWindow.from_dict(raw))
        return cls(
            provider=data["provider"],
            windows=windows,
            sampled_at=float(data["sampled_at"]),
            unreadable_windows=list(data.get("unreadable_windows") or []),
        )


@dataclass
class LandingConfig:
    min_reserve_percent: float = 3.0
    default_reserve_percent: float = 4.0
    max_reserve_percent: float = 15.0
    # Landing costs tool calls, not wall-clock time: reading enough state to be
    # accurate, checking git, and writing the handoff. Burn samples are therefore
    # normalized per tool call so the reserve does not drift with how often the
    # host happens to refresh the quota snapshot.
    landing_tool_calls: int = 8
    safety_factor: float = 1.5
    conserve_margin_percent: float = 5.0
    emergency_margin_percent: float = 0.75
    # A snapshot older than this cannot be treated as current headroom.
    max_snapshot_age_seconds: float = 900.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LandingConfig":
        # Tolerate configs written by older versions (e.g. the former
        # `landing_turns`) instead of raising on an unknown key.
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


@dataclass
class TaskSession:
    task: str
    provider: str
    window: str
    floor_percent: float
    started_remaining_percent: float
    mode: str = "preserve"
    started_at: float = field(default_factory=time)
    max_spend_percent: float | None = None
    handoff_path: str = "AGENT_HANDOFF.md"
    cwd: str = "."
    state: LandingState = LandingState.NORMAL
    last_notified_state: LandingState | None = None
    last_remaining_percent: float | None = None
    # Quota burned per tool call, not per snapshot interval.
    burn_samples: list[float] = field(default_factory=list)
    tool_calls_since_snapshot: int = 0
    config: LandingConfig = field(default_factory=LandingConfig)
    stop_reason: str | None = None
    completed_at_remaining_percent: float | None = None
    # The budget contract outlives any single task. Each completed task starts a
    # new segment under the same floor rather than releasing the guard.
    segment_count: int = 1
    previous_tasks: list[str] = field(default_factory=list)
    #: Remaining percent per window when the budget started, so `spend N` means
    #: "N points off *this* window" on every window rather than only the one the
    #: session happened to be pinned to.
    started_remaining_by_window: dict[str, float] = field(default_factory=dict)
    #: True when the floor guards every window (the `auto` default). Set False by
    #: naming a window explicitly, which scopes the floor to that one. Landing
    #: feasibility is still checked on every window either way -- that part is a
    #: safety property and is deliberately not opt-out-able.
    floor_applies_to_all_windows: bool = True

    @property
    def is_cancelled(self) -> bool:
        """The user deliberately ended the contract; the guard stands down.

        Distinct from every other stopped state: those mean "this work is over",
        while this means "stop guarding me at all". Kept on disk rather than
        deleted so burn samples and history survive for the next budget.
        """
        return self.state == LandingState.STOPPED and self.stop_reason == "cancelled"

    @property
    def is_renewable(self) -> bool:
        """A finished task can start a new segment; a landed budget cannot.

        `task_complete` means the task ended while quota was still healthy, so
        the floor was never in danger. `landed`/`emergency` mean the guard
        actually stopped the work, and renewing would defeat the floor.
        """
        return self.state == LandingState.STOPPED and self.stop_reason == "task_complete"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["last_notified_state"] = self.last_notified_state.value if self.last_notified_state else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSession":
        allowed = {f.name for f in fields(cls)}
        raw = {k: v for k, v in dict(data).items() if k in allowed}
        raw["state"] = LandingState(raw.get("state", LandingState.NORMAL.value))
        notified = raw.get("last_notified_state")
        raw["last_notified_state"] = LandingState(notified) if notified else None
        raw["config"] = LandingConfig.from_dict(raw.get("config", {}))
        return cls(**raw)


@dataclass
class PolicyDecision:
    state: LandingState
    remaining_percent: float
    floor_percent: float
    reserve_percent: float
    development_floor_percent: float
    reason: str
    snapshot_age_seconds: float | None = None
    stale: bool = False
    quota_known: bool = True
    #: Window that forced the state, when it was not the budget window.
    limiting_window: str | None = None

    def describe_remaining(self) -> str:
        """Never present an untrustworthy reading as the current number."""
        if not self.quota_known:
            return f"last known {self.remaining_percent:.1f}% (current quota unreadable)"
        if self.stale and self.snapshot_age_seconds is not None:
            return f"{self.remaining_percent:.1f}% as of {self.snapshot_age_seconds / 60:.0f} minutes ago"
        return f"{self.remaining_percent:.1f}%"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data
