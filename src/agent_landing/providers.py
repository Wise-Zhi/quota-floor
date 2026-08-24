from __future__ import annotations

import json
import subprocess
from time import time
from typing import Any

from .models import QuotaSnapshot, QuotaWindow


def parse_claude_statusline(payload: dict[str, Any]) -> QuotaSnapshot:
    rate_limits = payload.get("rate_limits") or {}
    windows: list[QuotaWindow] = []
    for window_id, raw in rate_limits.items():
        if not isinstance(raw, dict) or "used_percentage" not in raw:
            continue
        windows.append(
            QuotaWindow(
                id=window_id,
                used_percent=float(raw["used_percentage"]),
                resets_at=int(raw["resets_at"]) if raw.get("resets_at") is not None else None,
                source="claude-statusline",
                confidence="high",
            )
        )
    return QuotaSnapshot(provider="claude", windows=windows, sampled_at=time())


def _codex_window_id(duration_mins: int | None, position: str) -> str:
    if duration_mins == 300:
        return "five_hour"
    if duration_mins == 10080:
        return "seven_day"
    if duration_mins:
        return f"window_{duration_mins}m"
    return position


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

        rate_limits = result.get("rateLimits") or {}
        windows: list[QuotaWindow] = []
        for position in ("primary", "secondary"):
            raw = rate_limits.get(position)
            if not isinstance(raw, dict) or raw.get("usedPercent") is None:
                continue
            duration = raw.get("windowDurationMins")
            windows.append(
                QuotaWindow(
                    id=_codex_window_id(int(duration) if duration is not None else None, position),
                    used_percent=float(raw["usedPercent"]),
                    resets_at=int(raw["resetsAt"]) if raw.get("resetsAt") is not None else None,
                    source="codex-app-server",
                    confidence="high",
                )
            )
        return QuotaSnapshot(provider="codex", windows=windows, sampled_at=time())
