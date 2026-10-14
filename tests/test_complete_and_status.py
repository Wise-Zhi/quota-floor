"""A chat's budget must be completable by its agent and visible from a terminal.

Two gaps found while scripting the demo, both caused by the move to
per-conversation budgets:

1. The prompt hook told the agent to run `... -m agent_landing.cli complete`, but
   the gate denied it as a release, and the command itself had no conversation
   to act on -- it would have completed `sessions/default.json`. A finished
   budget stayed armed in the chat until `/landing cancel`.
2. Terminal `landing status` read only the default budget, so it reported
   "No active task" while a chat's budget was guarding.
"""

import io
import json
import sys
from pathlib import Path
from time import time

import pytest

from agent_landing.cli import main
from agent_landing.hooks import landing_command, pre_tool, user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/x.py"}, "tool_use_id": "w1"}


def bash(command: str, tool_use_id: str = "b1") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": tool_use_id}


def chat_budget(tmp_path: Path, chat: str = "c1", *, remaining: float = 90.0,
                floor: float = 40.0) -> StateStore:
    store = StateStore(tmp_path, session_id=chat)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    store.save_session(TaskSession(
        task=f"task in {chat}", provider="claude", window="seven_day",
        floor_percent=floor, started_remaining_percent=remaining, cwd=str(tmp_path),
    ))
    return store


def decision(result: dict) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def run_cli(monkeypatch, capsys, argv: list[str], stdin: str = "") -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["landing", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main()
    return code, capsys.readouterr().out


# --- 1. completing from the chat ---------------------------------------------

@pytest.mark.parametrize("command", [
    "python -m agent_landing.cli complete",
    "landing complete",
    landing_command("complete"),
])
def test_the_agent_can_complete_its_own_chat_budget(tmp_path, command):
    store = chat_budget(tmp_path)
    result = pre_tool(bash(command), store)

    assert store.load_session().is_complete
    # The guard did the work itself, against this conversation; the command it
    # intercepted must not run and complete some other budget.
    assert decision(result) == "deny"
    assert "complete" in result["hookSpecificOutput"]["permissionDecisionReason"].casefold()
    assert not (store.sessions_dir / "default.json").exists()


def test_completing_freezes_the_rest_of_the_turn(tmp_path):
    """Completion must never be a way to keep spending past the floor."""
    store = chat_budget(tmp_path)
    pre_tool(bash("landing complete"), store)
    assert decision(pre_tool(WRITE, store)) == "deny"
    assert decision(pre_tool(bash("git status", "b2"), store)) == "deny"


def test_the_next_user_prompt_releases_the_completed_budget(tmp_path):
    store = chat_budget(tmp_path)
    pre_tool(bash("landing complete"), store)
    user_prompt({"session_id": "c1", "prompt": "now something unrelated"}, store)
    assert store.load_session().is_inactive
    assert pre_tool(WRITE, store) == {}


def test_completing_one_chat_leaves_another_armed(tmp_path):
    mine = chat_budget(tmp_path, "c1")
    other = chat_budget(tmp_path, "c2")
    pre_tool(bash("landing complete"), mine)
    assert mine.load_session().is_complete
    assert not other.load_session().is_complete


@pytest.mark.parametrize("command", [
    "landing complete && rm -rf build",
    "landing complete; landing cancel",
    "landing complete $(rm -rf build)",
    "landing cancel complete",
    "landing complete --leave 5",
])
def test_only_the_bare_complete_command_completes(tmp_path, command):
    """Anything riding along with `complete` is still a release, and denied."""
    store = chat_budget(tmp_path)
    assert decision(pre_tool(bash(command), store)) == "deny"
    session = store.load_session()
    assert not session.is_complete and not session.is_inactive


def test_complete_past_the_boundary_does_not_skip_the_handoff(tmp_path):
    """In LAND the task is out of budget, not finished: the handoff still comes."""
    store = chat_budget(tmp_path, remaining=41.0, floor=40.0)
    result = pre_tool(bash("landing complete"), store)
    assert decision(result) == "deny"
    session = store.load_session()
    assert session.state == LandingState.LAND
    assert not session.is_complete


# --- 2. terminal status sees chat budgets ------------------------------------

def test_status_shows_a_chat_budget(tmp_path, monkeypatch, capsys):
    chat_budget(tmp_path)
    code, out = run_cli(monkeypatch, capsys, ["--cwd", str(tmp_path), "status"])
    assert code == 0
    payload = json.loads(out)
    assert payload["session"] == "c1"
    assert payload["task"] == "task in c1"
    assert payload["state"] == "normal"


def test_compact_status_shows_a_chat_budget(tmp_path, monkeypatch, capsys):
    chat_budget(tmp_path)
    code, out = run_cli(monkeypatch, capsys, ["--cwd", str(tmp_path), "status", "--compact"])
    assert code == 0
    assert out.startswith("AL | W 90.0->40.0%")


def test_status_shows_every_live_chat_budget(tmp_path, monkeypatch, capsys):
    chat_budget(tmp_path, "c1")
    chat_budget(tmp_path, "c2", floor=60.0)
    code, out = run_cli(monkeypatch, capsys, ["--cwd", str(tmp_path), "status"])
    assert code == 0
    assert {entry["session"] for entry in json.loads(out)} == {"c1", "c2"}

    code, out = run_cli(monkeypatch, capsys, ["--cwd", str(tmp_path), "status", "--compact"])
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert all(line.startswith("AL | ") for line in lines)


def test_status_ignores_released_chat_budgets(tmp_path, monkeypatch, capsys):
    store = chat_budget(tmp_path)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "completed_released"
    store.save_session(session)
    code, out = run_cli(monkeypatch, capsys, ["--cwd", str(tmp_path), "status"])
    assert code == 1
    assert "No active task" in out


# --- the reported repro, end to end through the CLI --------------------------

def test_reported_repro(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LANDING_HOME", str(tmp_path))
    statusline = json.dumps({
        "session_id": "c1",
        "rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": time() + 86400}},
    })
    run_cli(monkeypatch, capsys, ["claude-statusline"], statusline)
    run_cli(monkeypatch, capsys, ["hook-user-prompt", "--platform", "claude"],
            json.dumps({"session_id": "c1", "prompt": "/landing leave 40 x"}))

    code, out = run_cli(monkeypatch, capsys, ["status"])
    assert code == 0, out
    assert json.loads(out)["session"] == "c1"

    code, out = run_cli(monkeypatch, capsys, ["hook-pre-tool", "--platform", "claude"], json.dumps({
        "session_id": "c1", **bash("python -m agent_landing.cli complete"),
    }))
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "complete" in reason.casefold()
    assert StateStore(tmp_path, session_id="c1").load_session().is_complete
