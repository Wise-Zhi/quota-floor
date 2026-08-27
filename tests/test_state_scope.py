"""Where the budget lives.

Quota is account-wide, so the budget is too. State used to be per checkout,
which meant a floor set in one repository stopped guarding the moment the agent
worked in another -- against the same quota -- and each new checkout started its
burn history, and therefore its landing reserve, from scratch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_landing.cli import build_parser
from agent_landing.hooks import pre_tool
from agent_landing.models import QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore, adopt_project_state, resolve_state_root


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_state_is_user_scoped_by_default(fake_home, monkeypatch):
    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    assert resolve_state_root(cwd="/some/project") == fake_home / ".landing"


def test_project_scope_is_the_explicit_opt_out(fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    assert resolve_state_root(cwd=tmp_path, scope="project") == tmp_path.resolve() / ".landing"

    monkeypatch.setenv("LANDING_STATE_SCOPE", "project")
    assert resolve_state_root(cwd=tmp_path) == tmp_path.resolve() / ".landing"


def test_landing_home_still_wins_over_the_scope(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("LANDING_HOME", str(tmp_path / "explicit"))
    monkeypatch.setenv("LANDING_STATE_SCOPE", "project")
    assert resolve_state_root(cwd=tmp_path) == (tmp_path / "explicit").resolve()


def test_an_unknown_scope_is_refused_rather_than_guessed(monkeypatch):
    monkeypatch.setenv("LANDING_STATE_SCOPE", "global")
    with pytest.raises(ValueError, match="Unknown state scope"):
        resolve_state_root(cwd=".")


def test_one_budget_guards_work_in_a_second_checkout(fake_home, tmp_path, monkeypatch):
    """The failure this change exists to fix: repo B burning repo A's quota unguarded."""
    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    args = build_parser().parse_args([
        "--cwd", str(repo_a), "start", "--provider", "claude", "--window", "five_hour",
        "--leave", "70", "--remaining", "73", "--task", "t",
    ])
    assert args.func(args) == 0

    # A hook fired from the other checkout sees the same budget, and it binds.
    decision = pre_tool(
        {"tool_name": "Write", "tool_input": {"file_path": "x.py"}},
        StateStore(repo_b),
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_project_budget_is_adopted_into_the_user_root(fake_home, tmp_path, monkeypatch):
    """An upgrade must not look like the guard quietly forgetting the floor."""
    monkeypatch.setenv("LANDING_STATE_SCOPE", "project")
    project = StateStore(tmp_path)
    project.save_session(TaskSession(
        task="from the old layout",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=95,
        cwd=str(tmp_path),
    ))
    project.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=9.0)],
        sampled_at=1.0,
    ))

    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    user_root = fake_home / ".landing"
    assert adopt_project_state(tmp_path, user_root) == project.root
    assert StateStore(tmp_path).load_session().task == "from the old layout"
    # Copied, not moved: an upgrade that deletes a user's state is the worse trade.
    assert project.session_path.exists()


def test_adoption_never_overwrites_a_live_budget(fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("LANDING_STATE_SCOPE", "project")
    StateStore(tmp_path).save_session(TaskSession(
        task="old", provider="claude", window="seven_day",
        floor_percent=70, started_remaining_percent=95, cwd=str(tmp_path),
    ))

    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    user_store = StateStore(tmp_path)
    user_store.save_session(TaskSession(
        task="current", provider="claude", window="seven_day",
        floor_percent=70, started_remaining_percent=95, cwd=str(tmp_path),
    ))

    assert adopt_project_state(tmp_path, user_store.root) is None
    assert user_store.load_session().task == "current"


def test_the_scope_flag_overrides_the_environment(fake_home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANDING_STATE_SCOPE", "project")
    args = build_parser().parse_args([
        "--cwd", str(tmp_path), "--state-scope", "user", "start", "--provider", "claude",
        "--window", "five_hour", "--leave", "70", "--remaining", "90", "--task", "t",
    ])
    assert args.func(args) == 0
    capsys.readouterr()

    assert (fake_home / ".landing" / "session.json").exists()
    assert not (tmp_path / ".landing" / "session.json").exists()


def test_a_new_segment_adopts_the_directory_the_instruction_came_from(fake_home, tmp_path, monkeypatch, capsys):
    """One budget spans checkouts, so an emergency handoff must land where the work is."""
    monkeypatch.delenv("LANDING_STATE_SCOPE", raising=False)
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    for argv in (
        ["--cwd", str(repo_a), "start", "--provider", "claude", "--window", "five_hour",
         "--leave", "70", "--remaining", "95", "--task", "t"],
        ["--cwd", str(repo_a), "complete"],
        ["--cwd", str(repo_b), "resume"],
    ):
        args = build_parser().parse_args(argv)
        assert args.func(args) == 0
    capsys.readouterr()

    assert Path(StateStore(repo_b).load_session().cwd) == repo_b.resolve()
