from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


@dataclass
class QuotaSnapshot:
    provider: str
    windows: list[QuotaWindow]
    sampled_at: float

    def window(self, window_id: str) -> QuotaWindow | None:
        return next((w for w in self.windows if w.id == window_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "windows": [asdict(w) | {"remaining_percent": w.remaining_percent} for w in self.windows],
            "sampled_at": self.sampled_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaSnapshot":
        windows: list[QuotaWindow] = []
        for raw in data.get("windows", []):
            raw = dict(raw)
            raw.pop("remaining_percent", None)
            windows.append(QuotaWindow.from_dict(raw))
        return cls(provider=data["provider"], windows=windows, sampled_at=float(data["sampled_at"]))


@dataclass
class LandingConfig:
    min_reserve_percent: float = 3.0
    default_reserve_percent: float = 4.0
    max_reserve_percent: float = 15.0
    landing_turns: int = 3
    safety_factor: float = 1.5
    conserve_margin_percent: float = 5.0
    emergency_margin_percent: float = 0.75


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
    burn_samples: list[float] = field(default_factory=list)
    config: LandingConfig = field(default_factory=LandingConfig)
    stop_reason: str | None = None
    completed_at_remaining_percent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["last_notified_state"] = self.last_notified_state.value if self.last_notified_state else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSession":
        raw = dict(data)
        raw["state"] = LandingState(raw.get("state", LandingState.NORMAL.value))
        notified = raw.get("last_notified_state")
        raw["last_notified_state"] = LandingState(notified) if notified else None
        raw["config"] = LandingConfig(**raw.get("config", {}))
        return cls(**raw)


@dataclass
class PolicyDecision:
    state: LandingState
    remaining_percent: float
    floor_percent: float
    reserve_percent: float
    development_floor_percent: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data
