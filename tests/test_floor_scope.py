"""Which windows a floor guards, and what happens when one already breaches it.

`auto` (the default) applies the floor to every window the plan exposes and
stops at whichever reaches it first. Naming a window scopes the floor to that
one -- but landing feasibility is still checked everywhere, because a handoff
that cannot be written is a safety property, not a preference.
"""

import json
from pathlib import Path
from time import time

import pytest

from agent_landing.cli import build_parser
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore


def seed(tmp_path: Path, five_hour: float, seven_day: float) -> StateStore:
    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[
            QuotaWindow(id="five_hour", used_percent=100 - five_hour),
            QuotaWindow(id="seven_day", used_percent=100 - seven_day),
        ],
        sampled_at=time(),
    ))
    return store


def run(tmp_path: Path, *argv: str) -> dict:
    """Run a CLI command against an isolated store and return its JSON output."""
    args = build_parser().parse_args([*argv])
    return args


def test_manual_quota_must_name_its_window(tmp_path, capsys):
    """A single hand-written number cannot describe "every window"."""
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--leave", "70",
            "--task", "t", "--remaining", "90"]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert "--window five_hour" in str(excinfo.value)


def test_a_floor_already_breached_on_another_window_is_refused(tmp_path):
    """Opening the budget would demand an emergency landing on the first call."""
    seed(tmp_path, five_hour=55.0, seven_day=99.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--leave", "70",
            "--task", "t"]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    message = str(excinfo.value)
    assert "five_hour at 55.0%" in message
    # The refusal has to be actionable in the syntax the user actually typed.
    assert "leave weekly 70" in message


def test_spend_is_measured_per_window_by_the_preflight_check(tmp_path, capsys):
    """`spend N` is a delta on each window, not one absolute floor for all of them.

    The five-hour window is normally further burned than the weekly one, so
    comparing every window against the floor derived from the *budget* window
    refused ordinary `spend` invocations: with weekly at 90% and five-hour at
    20%, `spend 15` was rejected because five-hour sat below 90-15=75.
    """
    seed(tmp_path, five_hour=20.0, seven_day=90.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--spend", "15",
            "--task", "t"]
    args = build_parser().parse_args(argv)
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    # Since 2026-10-15 `spend` binds to the five-hour window when there is one;
    # the weekly window keeps only the feasibility check (test_spend_on_five_hour).
    assert payload["floor_applies_to"] == ["five_hour"]

    from agent_landing.policy import window_floor
    session = StateStore(tmp_path).load_session()
    # 15 points off where five-hour actually stood, not 75%.
    assert window_floor(session, "five_hour") == 5.0
    assert window_floor(session, "seven_day") == 0.0


def test_spend_is_still_refused_when_a_window_cannot_afford_it(tmp_path):
    """Per-window does not mean unchecked: a window at its own floor still refuses."""
    seed(tmp_path, five_hour=0.5, seven_day=90.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--spend", "15",
            "--task", "t"]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    assert "five_hour at 0.5%" in str(excinfo.value)


def test_a_refused_start_leaves_no_session_behind(tmp_path):
    """A refusal must not create the budget it just refused to create."""
    seed(tmp_path, five_hour=55.0, seven_day=99.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--leave", "70",
            "--task", "t"]
    args = build_parser().parse_args(argv)
    with pytest.raises(SystemExit):
        args.func(args)
    assert StateStore(tmp_path).load_session() is None


def test_scoping_the_floor_to_one_window_is_the_escape_hatch(tmp_path, capsys):
    seed(tmp_path, five_hour=55.0, seven_day=99.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--window", "seven_day",
            "--leave", "70", "--task", "t"]
    args = build_parser().parse_args(argv)
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["floor_applies_to"] == ["seven_day"]

    session = StateStore(tmp_path).load_session()
    assert session.floor_applies_to_all_windows is False


def test_the_default_floor_covers_every_window(tmp_path, capsys):
    seed(tmp_path, five_hour=94.0, seven_day=99.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--leave", "70",
            "--task", "t"]
    args = build_parser().parse_args(argv)
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["floor_applies_to"] == ["five_hour", "seven_day"]
    assert "every quota window" in payload["interpretation"]


def test_scoping_the_floor_does_not_disable_landing_feasibility(tmp_path):
    """The floor is the user's call; being able to write a handoff is not."""
    from agent_landing.hooks import evaluate_current

    store = seed(tmp_path, five_hour=94.0, seven_day=99.0)
    argv = ["--cwd", str(tmp_path), "start", "--provider", "claude", "--window", "seven_day",
            "--leave", "70", "--task", "t"]
    args = build_parser().parse_args(argv)
    args.func(args)

    # The scoped-out window collapses to nearly nothing.
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[
            QuotaWindow(id="five_hour", used_percent=98.0),
            QuotaWindow(id="seven_day", used_percent=1.0),
        ],
        sampled_at=time(),
    ))
    _, decision = evaluate_current(store)
    assert decision.state == LandingState.LAND
    assert decision.limiting_window == "five_hour"
