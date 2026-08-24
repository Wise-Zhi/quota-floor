from __future__ import annotations

import json
import os
from pathlib import Path
from time import time

from .models import QuotaSnapshot, TaskSession


class StateStore:
    def __init__(self, cwd: str | Path | None = None):
        project = Path(cwd or os.getcwd()).resolve()
        override = os.environ.get("LANDING_HOME") or os.environ.get("AGENT_LANDING_HOME")
        self.root = Path(override).expanduser().resolve() if override else project / ".landing"
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_path = self.root / "session.json"
        self.snapshot_path = self.root / "latest_snapshot.json"
        self.history_path = self.root / "snapshots.jsonl"

    def save_session(self, session: TaskSession) -> None:
        self.session_path.write_text(json.dumps(session.to_dict(), indent=2) + "\n", encoding="utf-8")

    def load_session(self) -> TaskSession | None:
        if not self.session_path.exists():
            return None
        return TaskSession.from_dict(json.loads(self.session_path.read_text(encoding="utf-8")))

    def save_snapshot(self, snapshot: QuotaSnapshot) -> None:
        payload = snapshot.to_dict()
        self.snapshot_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

        session = self.load_session()
        if not session or session.provider != snapshot.provider:
            return
        window = snapshot.window(session.window)
        if not window:
            return
        remaining = window.remaining_percent
        if session.last_remaining_percent is not None:
            burn = session.last_remaining_percent - remaining
            # Ignore resets/increases and microscopic reporting noise.
            if 0.01 <= burn <= 25.0:
                session.burn_samples.append(round(burn, 4))
                session.burn_samples = session.burn_samples[-24:]
        session.last_remaining_percent = remaining
        self.save_session(session)


    def load_history(self, limit: int = 200) -> list[QuotaSnapshot]:
        if not self.history_path.exists():
            return []
        snapshots: list[QuotaSnapshot] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                snapshots.append(QuotaSnapshot.from_dict(json.loads(line)))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return snapshots

    def load_snapshot(self) -> QuotaSnapshot | None:
        if not self.snapshot_path.exists():
            return None
        return QuotaSnapshot.from_dict(json.loads(self.snapshot_path.read_text(encoding="utf-8")))

    def snapshot_age_seconds(self) -> float | None:
        snapshot = self.load_snapshot()
        return None if not snapshot else max(0.0, time() - snapshot.sampled_at)
