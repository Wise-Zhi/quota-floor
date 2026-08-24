from pathlib import Path

from agent_landing.dashboard import DASHBOARD_HTML, status_payload
from agent_landing.models import QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore


def test_dashboard_payload_keeps_floor_distinct_from_runway(tmp_path: Path):
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="finish auth",
        provider="claude",
        window="seven_day",
        floor_percent=75,
        started_remaining_percent=95,
        started_at=0,
        last_remaining_percent=95,
        cwd=str(tmp_path),
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=5)],
        sampled_at=1,
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=10)],
        sampled_at=3601,
    ))
    payload = status_payload(store)
    assert payload["floor_percent"] == 75
    assert payload["remaining_percent"] == 90
    assert payload["safe_development_headroom_percent"] > 0
    assert payload["runway"]["available"]
    assert "never a spending target" in payload["invariant"]


def test_dashboard_html_uses_burn_rate_runway_not_task_cost_prediction():
    assert "Observed burn-rate runway" in DASHBOARD_HTML
    assert "No task-cost guess" in DASHBOARD_HTML
    assert "never a spending target" in DASHBOARD_HTML
