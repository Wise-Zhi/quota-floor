"""In LAND the model's final reply is the handoff, and the Stop hook saves it.

The old design had the model write `AGENT_HANDOFF.md` into the working folder,
which meant the gate needed a hole for exactly one write -- the hole a Codex
`apply_patch` bug once closed, leaving a budget that could enter LAND and never
land. Now LAND denies every write, and nothing lands in the user's folder.
Both hosts hand the Stop hook the final reply as `last_assistant_message`.
"""

from pathlib import Path

from agent_landing.hooks import post_tool, pre_tool, stop
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore

HANDOFF_REPLY = "## Completed\n- auth model\n## Next steps\n1. wire the login route"


def store_in_land(tmp_path: Path, remaining: float = 73.5) -> StateStore:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    store = StateStore(project)
    store.save_session(TaskSession(
        task="implement auth",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=90,
        cwd=str(project),
    ))
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=1,
    ))
    return store


def enter_land(store: StateStore) -> dict:
    return pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)


def test_land_denies_every_write_including_a_handoff_file(tmp_path):
    store = store_in_land(tmp_path)
    assert enter_land(store)["hookSpecificOutput"]["permissionDecision"] == "deny"
    for path in ("AGENT_HANDOFF.md", str(store.handoffs_dir / "x.md")):
        result = pre_tool({"tool_name": "Write", "tool_input": {"file_path": path}}, store)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", path


def test_land_denies_a_codex_patch_that_only_adds_a_handoff(tmp_path):
    store = store_in_land(tmp_path)
    enter_land(store)
    patch = "*** Begin Patch\n*** Add File: AGENT_HANDOFF.md\n+# handoff\n*** End Patch\n"
    result = pre_tool({"tool_name": "apply_patch", "tool_input": {"command": patch}}, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_land_instruction_asks_for_the_handoff_as_the_final_reply(tmp_path):
    store = store_in_land(tmp_path)
    context = enter_land(store)["hookSpecificOutput"]["additionalContext"]
    assert "final reply" in context
    assert "AGENT_HANDOFF" not in context


def test_the_final_reply_is_saved_and_the_task_lands(tmp_path):
    store = store_in_land(tmp_path)
    enter_land(store)

    result = stop({"hook_event_name": "Stop", "last_assistant_message": HANDOFF_REPLY}, store)

    session = store.load_session()
    assert session.state == LandingState.STOPPED
    assert session.stop_reason == "landed"
    saved = Path(session.handoff_path)
    assert saved.parent == store.handoffs_dir
    assert HANDOFF_REPLY in saved.read_text(encoding="utf-8")
    assert str(saved) in result["systemMessage"]


def test_nothing_is_written_into_the_working_folder(tmp_path):
    store = store_in_land(tmp_path)
    enter_land(store)
    stop({"last_assistant_message": HANDOFF_REPLY}, store)

    project = tmp_path / "project"
    leftovers = [p for p in project.rglob("*") if ".landing" not in p.parts]
    assert leftovers == []


def test_an_empty_final_reply_still_lands_with_the_fallback(tmp_path):
    """No reply to save is not a reason to leave the user with nothing."""
    store = store_in_land(tmp_path)
    enter_land(store)
    stop({"last_assistant_message": "   "}, store)

    session = store.load_session()
    assert session.stop_reason == "landed"
    assert "Emergency Fallback" in Path(session.handoff_path).read_text(encoding="utf-8")


def test_stop_outside_land_releases_without_a_handoff(tmp_path):
    """Outside LAND the turn simply ends the budget; there is nothing to hand off."""
    store = store_in_land(tmp_path, remaining=95)
    pre_tool({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, store)

    assert stop({"last_assistant_message": "done"}, store) == {}
    session = store.load_session()
    assert session.stop_reason == "completed_released"
    assert session.is_inactive
    assert not store.handoffs_dir.exists()


def test_stop_with_no_budget_is_a_no_op(tmp_path):
    assert stop({"last_assistant_message": "hi"}, StateStore(tmp_path)) == {}


def test_a_land_the_model_was_never_told_about_asks_for_the_handoff_first(tmp_path):
    """LAND can be reached by a reading that arrived after the last tool call, so
    the model may end its turn never having been told to land. Saving that reply
    would file an ordinary answer as the handoff; give it the turn instead."""
    store = store_in_land(tmp_path)
    session = store.load_session()
    session.state = LandingState.LAND
    store.save_session(session)

    result = stop({"last_assistant_message": "Here is the refactor."}, store)

    assert result["decision"] == "block"
    assert "final reply" in result["reason"]
    assert store.load_session().state == LandingState.LAND

    # The second stop saves whatever it gets rather than looping.
    stop({"last_assistant_message": HANDOFF_REPLY, "stop_hook_active": True}, store)
    assert store.load_session().stop_reason == "landed"


def test_the_retired_post_tool_hook_never_lands_a_task(tmp_path):
    """Sessions started before the upgrade still run it until restarted."""
    store = store_in_land(tmp_path)
    enter_land(store)
    (tmp_path / "project" / "AGENT_HANDOFF.md").write_text("# handoff", encoding="utf-8")
    assert post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store) == {}
    assert store.load_session().state == LandingState.LAND


def test_emergency_writes_its_fallback_outside_the_working_folder(tmp_path):
    store = store_in_land(tmp_path, remaining=70.4)
    result = enter_land(store)

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    session = store.load_session()
    assert session.state == LandingState.STOPPED
    saved = Path(session.handoff_path)
    assert saved.parent == store.handoffs_dir and saved.exists()
    assert str(saved) in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert not (tmp_path / "project" / "AGENT_HANDOFF.md").exists()
