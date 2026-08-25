from __future__ import annotations

import json
import subprocess
from time import time
from typing import Any

from .models import QuotaSnapshot, QuotaWindow


_USED_KEYS = ("used_percentage", "usedPercent", "used_percent")
_REMAINING_KEYS = ("remaining_percentage", "remainingPercent", "remaining_percent", "percent_left")


def read_quota_percent(raw: dict[str, Any]) -> tuple[float | None, str | None]:
    """Normalize a provider payload to *used* percent, the one internal unit.

    Polarity is decided by the field NAME and never by the value: "30" is
    identical whether it means 30% spent or 30% left, so a payload whose polarity
    cannot be established is reported as unknown rather than guessed. Unknown is
    not 100% available.

    Returns ``(used_percent, problem)``. When the value cannot be trusted the
    problem string explains why, so the window can be reported as unreadable
    instead of silently vanishing -- a window that disappears without explanation
    also silently drops its landing-feasibility check.
    """
    used = next((raw[key] for key in _USED_KEYS if raw.get(key) is not None), None)
    remaining = next((raw[key] for key in _REMAINING_KEYS if raw.get(key) is not None), None)

    if used is not None and remaining is not None:
        # Both polarities present: they must be complements, or the payload is
        # internally inconsistent and must not be trusted in either direction.
        if abs((float(used) + float(remaining)) - 100.0) > 1.0:
            return None, (
                f"contradictory quota fields (used {float(used):g}% and "
                f"remaining {float(remaining):g}% do not add up to 100%)"
            )
        return float(used), None
    if used is not None:
        return float(used), None
    if remaining is not None:
        return 100.0 - float(remaining), None
    return None, "no recognized used/remaining percentage field"


def used_percent_from(raw: dict[str, Any]) -> float | None:
    return read_quota_percent(raw)[0]


def parse_claude_statusline(payload: dict[str, Any]) -> QuotaSnapshot:
    rate_limits = payload.get("rate_limits") or {}
    windows: list[QuotaWindow] = []
    unreadable: list[dict[str, str]] = []
    for window_id, raw in rate_limits.items():
        if not isinstance(raw, dict):
            unreadable.append({"id": str(window_id), "reason": "quota entry was not an object"})
            continue
        used, problem = read_quota_percent(raw)
        if used is None:
            unreadable.append({"id": str(window_id), "reason": problem or "unreadable quota"})
            continue
        windows.append(
            QuotaWindow(
                id=window_id,
                used_percent=used,
                resets_at=int(raw["resets_at"]) if raw.get("resets_at") is not None else None,
                source="claude-statusline",
                confidence="high",
            )
        )
    return QuotaSnapshot(
        provider="claude", windows=windows, sampled_at=time(), unreadable_windows=unreadable
    )


def _codex_window_id(duration_mins: int | None, position: str) -> str:
    if duration_mins == 300:
        return "five_hour"
    if duration_mins == 10080:
        return "seven_day"
    if duration_mins:
        return f"window_{duration_mins}m"
    return position


def parse_codex_rate_limits(result: dict[str, Any]) -> QuotaSnapshot:
    """Turn an `account/rateLimits/read` result into a snapshot.

    Split out of the provider so it can be pinned against a recorded payload:
    while this lived inside `read()` it could only be exercised by spawning a
    real `codex app-server`, which is exactly why the field names went
    unverified until a live payload was captured by hand.
    """
    rate_limits = result.get("rateLimits") or {}
    windows: list[QuotaWindow] = []
    unreadable: list[dict[str, str]] = []
    for position in ("primary", "secondary"):
        raw = rate_limits.get(position)
        if not isinstance(raw, dict):
            continue
        duration_raw = raw.get("windowDurationMins")
        window_id = _codex_window_id(
            int(duration_raw) if duration_raw is not None else None, position
        )
        used, problem = read_quota_percent(raw)
        if used is None:
            unreadable.append({"id": window_id, "reason": problem or "unreadable quota"})
            continue
        windows.append(
            QuotaWindow(
                id=window_id,
                used_percent=used,
                resets_at=int(raw["resetsAt"]) if raw.get("resetsAt") is not None else None,
                source="codex-app-server",
                confidence="high",
            )
        )
    return QuotaSnapshot(
        provider="codex", windows=windows, sampled_at=time(), unreadable_windows=unreadable
    )


class CodexAppServerProvider:
    """Reads quota without invoking a model turn via Codex app-server JSON-RPC."""

    def __init__(self, executable: str = "codex", timeout_seconds: float = 8.0):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def _read_response(self, proc: subprocess.Popen[str], request_id: int) -> dict[str, Any]:
        # app-server is line-delimited JSON over stdio. Ignore notifications until our id arrives.
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                raise RuntimeError("codex app-server closed before returning quota")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"codex app-server error: {message['error']}")
                return message.get("result") or {}

    def read(self) -> QuotaSnapshot:
        proc = subprocess.Popen(
            [self.executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            assert proc.stdin is not None
            init = {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "agent_landing",
                        "title": "Agent Landing",
                        "version": "0.1.0",
                    }
                },
            }
            proc.stdin.write(json.dumps(init) + "\n")
            proc.stdin.flush()
            self._read_response(proc, 1)
            proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
            proc.stdin.write(json.dumps({"method": "account/rateLimits/read", "id": 2}) + "\n")
            proc.stdin.flush()
            result = self._read_response(proc, 2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

        return parse_codex_rate_limits(result)
