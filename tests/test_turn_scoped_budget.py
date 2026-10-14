"""A budget covers one instruction and ends by itself when that turn ends.

Before, a budget lived until the agent ran `landing complete`, the quota ran
out, or the user cancelled. An agent that finished without running `complete`
left the floor armed in the chat, and live budgets each claim a landing reserve
from the shared quota -- two forgotten ones pushed a fresh `spend 5` straight
into LAND (ChatGPT Work mode, 2026-10-15).

Now the first Stop after a budget opens releases it. Nothing on the prompt side
ends a budget except an explicit cancel, because a host may deliver a message
the user adds mid-task as a prompt, and releasing there would leave the rest of
the task silently unguarded. An interrupt fires no Stop (Claude Code documents
this), so the budget then carries over and ends with the next turn instead.
"""

from time import time

from agent_landing.hooks import pre_tool, stop, user_prompt
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/x.py"}}


def store_with_budget(tmp_path, remaining: float = 90.0, prompt: str = "/landing spend 20 fix it") -> StateStore:
    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="claude", windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    user_prompt({"prompt": prompt, "cwd": str(tmp_path)}, store)
    assert store.load_session() is not None
    return store


def is_guarding(store: StateStore) -> bool:
    session = store.load_session()
    return session is not None and not session.is_inactive


def test_the_end_of_the_turn_releases_the_budget(tmp_path):
    store = store_with_budget(tmp_path)
    pre_tool(WRITE, store)

    assert stop({"last_assistant_message": "Fixed it."}, store) == {}
    assert not is_guarding(store)
    assert pre_tool(WRITE, store) == {}


def test_a_conserving_budget_is_released_too(tmp_path):
    store = store_with_budget(tmp_path, remaining=22.0, prompt="/landing leave 15 fix it")
    pre_tool(WRITE, store)
    assert store.load_session().state == LandingState.CONSERVE

    stop({"last_assistant_message": "Fixed it."}, store)
    assert not is_guarding(store)


def test_a_budget_the_agent_already_completed_is_released_at_the_end_of_the_turn(tmp_path):
    store = store_with_budget(tmp_path)
    pre_tool({"tool_name": "Bash", "tool_input": {"command": "landing complete"}}, store)
    assert store.load_session().is_complete

    stop({"last_assistant_message": "Done."}, store)
    assert not is_guarding(store)


def test_a_message_added_mid_task_never_ends_the_budget(tmp_path):
    """Releasing here would leave the rest of the task silently unguarded."""
    store = store_with_budget(tmp_path)
    pre_tool(WRITE, store)

    user_prompt({"prompt": "also fix the header while you are at it"}, store)
    assert is_guarding(store)


def test_after_an_interrupt_the_budget_ends_with_the_next_turn(tmp_path):
    """An interrupt fires no Stop, so the budget carries over -- for one turn."""
    store = store_with_budget(tmp_path)
    pre_tool(WRITE, store)
    # The user pressed stop: no Stop event. Then a new instruction.
    user_prompt({"prompt": "never mind, do something else"}, store)
    assert is_guarding(store)

    stop({"last_assistant_message": "Did something else."}, store)
    assert not is_guarding(store)


def test_land_still_lands_with_a_handoff_rather_than_just_releasing(tmp_path):
    store = store_with_budget(tmp_path, remaining=73.5, prompt="/landing leave 70 fix it")
    pre_tool(WRITE, store)
    assert store.load_session().state == LandingState.LAND

    stop({"last_assistant_message": "## Next steps\n1. finish"}, store)
    session = store.load_session()
    assert session.stop_reason == "landed"
    assert session.handoff_path


def test_a_new_budget_after_a_released_one_starts_fresh(tmp_path):
    store = store_with_budget(tmp_path)
    stop({"last_assistant_message": "Fixed it."}, store)

    user_prompt({"prompt": "/landing spend 10 next thing"}, store)
    session = store.load_session()
    assert is_guarding(store)
    assert session.max_spend_percent == 10.0


def test_the_agent_is_not_told_to_run_complete_any_more(tmp_path):
    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider="claude", windows=[QuotaWindow(id="seven_day", used_percent=10)], sampled_at=time(),
    ))
    context = user_prompt({"prompt": "/landing spend 20 fix it", "cwd": str(tmp_path)}, store)[
        "hookSpecificOutput"]["additionalContext"]
    assert "complete" not in context
    assert "ends by itself" in context
