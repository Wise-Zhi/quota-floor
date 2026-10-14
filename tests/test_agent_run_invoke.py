"""An agent that runs `landing invoke` itself must open *this conversation's* budget.

Observed 2026-10-15 in ChatGPT Work mode: the hook missed a wrapped prompt, so
the agent ran `& .../landing.exe invoke '$landing spend 15 ...' --provider codex;
Get-Content ...` itself. A terminal command has no conversation, so the budget
opened on the default slot, which this conversation's gate never consults: the
task ran unenforced, and stayed under its cap only because the agent behaved.

The gate now handles a bare `landing invoke` itself, against the payload's
session, the way it handles `landing complete`. Anything that runs invoke or
start in any other shape is denied, whether or not a budget exists, so the CLI
can never quietly open a budget nobody enforces. Lowering a floor from here
stays refused: an agent's tool call is not the user.
"""

from time import time

import pytest

from agent_landing.hooks import pre_tool, user_prompt
from agent_landing.models import QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore

EXE = "C:/Users/user/Desktop/Landing/.venv/Scripts/landing.exe"


def store_with_quota(tmp_path, session_id="chat-1") -> StateStore:
    store = StateStore(tmp_path, session_id=session_id)
    store.save_snapshot(QuotaSnapshot(
        provider="codex",
        windows=[QuotaWindow(id="five_hour", used_percent=40), QuotaWindow(id="seven_day", used_percent=60)],
        sampled_at=time(),
    ))
    return store


def shell(command: str) -> dict:
    return {"tool_name": "exec_command", "tool_input": {"cmd": command}, "session_id": "chat-1"}


def reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    f"& {EXE} invoke '$landing spend 15 这些地方和原版的不一样' --provider codex",
    f"{EXE} invoke \"spend 15 这些地方和原版的不一样\" --provider codex",
    "landing invoke 'spend 15 这些地方和原版的不一样'",
    "python -m agent_landing.cli invoke 'spend 15 这些地方和原版的不一样' --provider codex",
])
def test_a_bare_invoke_opens_this_conversations_budget(tmp_path, command):
    store = store_with_quota(tmp_path)
    result = pre_tool(shell(command), store)

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "opened" in reason(result)
    session = store.load_session()
    assert (session.mode, session.max_spend_percent, session.task) == ("max_spend", 15.0, "这些地方和原版的不一样")
    assert session.window == "five_hour"
    # Nothing landed on the terminal's default budget.
    assert StateStore(tmp_path).load_session() is None


def test_an_invoke_chained_to_other_work_is_denied_and_opens_nothing(tmp_path):
    store = store_with_quota(tmp_path)
    result = pre_tool(shell(
        f"& {EXE} invoke '$landing spend 15 x' --provider codex; Get-Content public/js/a.js"
    ), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "on its own" in reason(result)
    assert store.load_session() is None


def test_start_from_a_tool_call_is_denied(tmp_path):
    store = store_with_quota(tmp_path)
    result = pre_tool(shell("landing start --provider codex --spend 15 --task x"), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.load_session() is None


def test_an_unparseable_invoke_is_denied_with_the_refusal(tmp_path):
    store = store_with_quota(tmp_path)
    result = pre_tool(shell("landing invoke '70 fix it'"), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "leave or quota to spend" in reason(result)
    assert store.load_session() is None


def test_an_agent_cannot_lower_a_floor_the_user_set(tmp_path):
    store = store_with_quota(tmp_path)
    user_prompt({"prompt": "$landing leave 30 x", "session_id": "chat-1", "cwd": str(tmp_path)}, store,
                provider="codex")
    result = pre_tool(shell("landing invoke 'leave 10 x'"), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.load_session().floor_percent == 30.0


def test_an_agent_cannot_cancel_through_invoke(tmp_path):
    store = store_with_quota(tmp_path)
    user_prompt({"prompt": "$landing leave 30 x", "session_id": "chat-1", "cwd": str(tmp_path)}, store,
                provider="codex")
    result = pre_tool(shell("landing invoke cancel"), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not store.load_session().is_inactive


def test_complete_with_no_budget_here_touches_nothing(tmp_path):
    """It used to fall through to the CLI and complete the terminal's budget."""
    store = store_with_quota(tmp_path)
    result = pre_tool(shell("landing complete"), store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert StateStore(tmp_path).load_session() is None


def test_ordinary_commands_mentioning_landing_are_untouched(tmp_path):
    store = store_with_quota(tmp_path)
    assert pre_tool(shell("rg -n landing README.md"), store) == {}
    assert pre_tool(shell("Get-Content docs/landing-start.md"), store) == {}
