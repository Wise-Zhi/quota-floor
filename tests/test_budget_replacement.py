"""What a second `/landing` does to the budget already in force.

`landing start` used to overwrite whatever session existed, silently and
completely. Two consequences, both bad in the same direction: an agent could
replace a 70% floor with 5% and keep working -- the gate only guards the
boundary, so in NORMAL nothing stopped it -- and the measured burn history was
thrown away, resetting the landing reserve from a real number to the 4% default.

The rule now follows the one rlimits use: a floor ratchets. Tightening needs no
permission; loosening needs `/landing cancel`, which only a real user prompt can
perform. It is deliberately not a `--force` flag, because a flag is something
the agent can type, and that is precisely what must not work.
"""

import json
from pathlib import Path
from time import time

import pytest

from agent_landing.cli import build_parser
from agent_landing.hooks import user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore


def seed(tmp_path: Path, remaining: float = 90.0) -> StateStore:
    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="five_hour", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    return store


def start(tmp_path: Path, floor: float, task: str = "t") -> int:
    args = build_parser().parse_args([
        "--cwd", str(tmp_path), "start", "--provider", "claude", "--window", "five_hour",
        "--leave", str(floor), "--remaining", "90", "--task", task,
    ])
    return args.func(args)


def with_measured_burn(store: StateStore) -> None:
    session = store.load_session()
    session.burn_samples = [0.9, 1.1, 0.8, 1.2]
    store.save_session(session)


def test_a_second_landing_cannot_lower_the_floor(tmp_path, capsys):
    """The hole this file exists for: the gate does not guard NORMAL."""
    seed(tmp_path)
    start(tmp_path, 70)
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        start(tmp_path, 5, task="agent task")

    message = str(excinfo.value)
    assert "70.0% floor" in message
    assert "Refusing to lower it to 5.0%" in message
    # A refusal with no way forward is its own trap.
    assert "/landing cancel" in message
    assert StateStore(tmp_path).load_session().floor_percent == 70


def test_tightening_the_floor_needs_no_permission(tmp_path, capsys):
    """Only one direction is dangerous, so only one direction is gated."""
    seed(tmp_path)
    start(tmp_path, 70)
    capsys.readouterr()

    assert start(tmp_path, 85) == 0
    assert StateStore(tmp_path).load_session().floor_percent == 85


def test_cancelling_first_is_the_way_to_loosen(tmp_path, capsys):
    """The escape is user-authenticated, which a flag would not be."""
    store = seed(tmp_path)
    start(tmp_path, 70)
    capsys.readouterr()

    user_prompt({"prompt": "/landing cancel"}, store)
    assert start(tmp_path, 5) == 0
    assert store.load_session().floor_percent == 5


def test_completed_budget_can_be_replaced_with_a_fresh_lower_floor(tmp_path, capsys):
    store = seed(tmp_path)
    start(tmp_path, 88)
    capsys.readouterr()
    session = store.load_session()
    session.floor_percent = 88
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    store.save_session(session)

    assert start(tmp_path, 70, task="new task") == 0
    capsys.readouterr()
    assert store.load_session().floor_percent == 70


def test_measured_burn_survives_a_new_budget(tmp_path, capsys):
    """Reserve accuracy belongs to the account, not to one budget.

    Dropping it reset a measured reserve to the 4% default, which starts the
    landing too late to finish a handoff -- the reserve biased downwards, the
    unsafe direction.
    """
    store = seed(tmp_path)
    start(tmp_path, 70)
    with_measured_burn(store)
    capsys.readouterr()

    start(tmp_path, 85)
    payload = json.loads(capsys.readouterr().out)

    assert store.load_session().burn_samples == [0.9, 1.1, 0.8, 1.2]
    assert payload["initial_landing_reserve_percent"] > 10, "measured reserve was discarded"
    assert payload["replaced_budget"]["burn_samples_carried_over"] == 4


def test_a_replacement_is_reported_not_inferred(tmp_path, capsys):
    """"If you change state, tell the user." Losing a budget is not inferable."""
    store = seed(tmp_path)
    start(tmp_path, 70, task="the original task")
    capsys.readouterr()

    user_prompt({"prompt": "/landing cancel"}, store)
    start(tmp_path, 5, task="the new task")
    replaced = json.loads(capsys.readouterr().out)["replaced_budget"]

    assert replaced["previous_floor_percent"] == 70.0
    assert replaced["previous_task"] == "the original task"
    assert replaced["was_cancelled"] is True


def test_the_first_budget_reports_nothing_replaced(tmp_path, capsys):
    seed(tmp_path)
    start(tmp_path, 70)
    assert json.loads(capsys.readouterr().out)["replaced_budget"] is None


def test_a_new_budget_is_segment_one(tmp_path, capsys):
    """A new contract honestly starts over, even though its burn history does not."""
    store = seed(tmp_path)
    start(tmp_path, 70)
    session = store.load_session()
    session.segment_count = 3
    store.save_session(session)
    capsys.readouterr()

    start(tmp_path, 85)
    assert store.load_session().segment_count == 1


def test_start_still_recovers_a_damaged_session(tmp_path, capsys):
    """The ratchet must not block the documented way out of a denied gate."""
    store = seed(tmp_path)
    start(tmp_path, 70)
    capsys.readouterr()
    store.session_path.write_text('{"task": "trunc', encoding="utf-8")

    assert start(tmp_path, 5, task="recovered") == 0
    assert store.load_session().task == "recovered"
