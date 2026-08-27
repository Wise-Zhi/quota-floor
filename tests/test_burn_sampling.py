"""Burn samples are priced per tool call, not per snapshot interval."""

from pathlib import Path
from time import time

from agent_landing.models import LandingConfig, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.policy import estimate_landing_reserve
from agent_landing.state import StateStore


def make_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="x",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=100,
        last_remaining_percent=100.0,
        cwd=str(tmp_path),
    ))
    return store


def push(store: StateStore, remaining: float, tool_calls: int) -> None:
    """Simulate `tool_calls` PreToolUse hits, then a quota snapshot."""
    session = store.load_session()
    session.tool_calls_since_snapshot += tool_calls
    store.save_session(session)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time(),
    ))


def test_burn_sample_is_divided_by_the_tool_calls_that_caused_it(tmp_path):
    store = make_store(tmp_path)
    push(store, 96.0, tool_calls=4)  # 4 points over 4 calls => 1.0 per call
    assert store.load_session().burn_samples == [1.0]


def test_reserve_is_independent_of_snapshot_refresh_rate(tmp_path):
    """The same work sampled coarsely or finely must yield the same per-call rate.

    This is the regression for the old behaviour, where the reserve silently
    scaled with how often the host happened to call the statusLine command.
    """
    coarse = make_store(tmp_path / "coarse")
    push(coarse, 94.0, tool_calls=6)   # one snapshot covering 6 calls
    push(coarse, 88.0, tool_calls=6)

    fine = make_store(tmp_path / "fine")
    for step in range(1, 13):          # twelve snapshots covering 1 call each
        push(fine, 100.0 - step, tool_calls=1)

    assert coarse.load_session().burn_samples == [1.0, 1.0]
    assert fine.load_session().burn_samples == [1.0] * 12

    cfg = LandingConfig()
    assert estimate_landing_reserve(coarse.load_session().burn_samples, cfg) == \
           estimate_landing_reserve(fine.load_session().burn_samples, cfg)


def test_snapshot_without_tool_calls_does_not_create_a_sample(tmp_path):
    store = make_store(tmp_path)
    push(store, 98.0, tool_calls=0)
    assert store.load_session().burn_samples == []


def test_unattributable_movement_keeps_accumulating_against_one_baseline(tmp_path):
    """Sub-threshold movement must not be dropped, or the reserve underestimates."""
    store = make_store(tmp_path)
    push(store, 100.0, tool_calls=3)   # no measurable movement yet
    assert store.load_session().burn_samples == []
    assert store.load_session().tool_calls_since_snapshot == 3

    push(store, 99.0, tool_calls=5)    # 1 point across all 8 calls
    assert store.load_session().burn_samples == [0.125]


def test_quota_reset_restarts_accounting_instead_of_sampling(tmp_path):
    store = make_store(tmp_path)
    push(store, 100.0, tool_calls=2)
    push(store, 100.0, tool_calls=0)
    session = store.load_session()
    session.last_remaining_percent = 40.0
    store.save_session(session)

    push(store, 95.0, tool_calls=3)    # quota went UP: a reset, not negative burn
    session = store.load_session()
    assert session.burn_samples == []
    assert session.last_remaining_percent == 95.0
    assert session.tool_calls_since_snapshot == 0


def test_reserve_scales_with_landing_tool_calls(tmp_path):
    samples = [0.5, 0.5, 0.6, 0.5]
    cheap = LandingConfig(landing_tool_calls=2, max_reserve_percent=99.0)
    costly = LandingConfig(landing_tool_calls=16, max_reserve_percent=99.0)
    assert estimate_landing_reserve(samples, costly) > estimate_landing_reserve(samples, cheap)


def test_one_tool_call_counts_once_even_if_two_hook_layers_fire(tmp_path):
    """Codex loads hooks from user scope *and* a project `.codex/hooks.json`.

    Both fire for a single tool call. Counting each one halves the measured
    per-tool-call burn, which halves the landing reserve and makes the agent
    land later than it can afford to -- a silent under-estimate, which is the
    dangerous direction.
    """
    from agent_landing.hooks import pre_tool

    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="codex",
        windows=[QuotaWindow(id="five_hour", used_percent=10.0)],
        sampled_at=time(),
    ))
    store.save_session(TaskSession(
        task="t", provider="codex", window="five_hour", floor_percent=50,
        started_remaining_percent=90, cwd=str(tmp_path),
    ))
    payload = {
        "tool_name": "Read", "tool_input": {"file_path": "x.py"},
        "tool_use_id": "call_same",
    }
    pre_tool(payload, store)
    pre_tool(payload, store)
    assert store.load_session().tool_calls_since_snapshot == 1

    pre_tool({**payload, "tool_use_id": "call_next"}, store)
    assert store.load_session().tool_calls_since_snapshot == 2


def test_a_host_that_sends_no_tool_use_id_still_counts(tmp_path):
    """Missing identity must over-estimate the reserve, never under-estimate it."""
    from agent_landing.hooks import pre_tool

    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="codex",
        windows=[QuotaWindow(id="five_hour", used_percent=10.0)],
        sampled_at=time(),
    ))
    store.save_session(TaskSession(
        task="t", provider="codex", window="five_hour", floor_percent=50,
        started_remaining_percent=90, cwd=str(tmp_path),
    ))
    payload = {"tool_name": "Read", "tool_input": {"file_path": "x.py"}}
    pre_tool(payload, store)
    pre_tool(payload, store)
    assert store.load_session().tool_calls_since_snapshot == 2
