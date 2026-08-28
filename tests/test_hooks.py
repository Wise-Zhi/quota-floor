from pathlib import Path

from agent_landing.hooks import post_tool, pre_tool
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore


def setup_store(tmp_path: Path, remaining: float) -> StateStore:
    store = StateStore(tmp_path)
    s = TaskSession(
        task="implement auth",
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=90,
        cwd=str(tmp_path),
    )
    store.save_session(s)
    store.save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=100 - remaining)],
        sampled_at=1,
    ))
    return store


def test_land_blocks_new_write_but_allows_handoff(tmp_path):
    store = setup_store(tmp_path, 73.5)
    blocked = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"

    allowed = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    assert allowed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_land_allows_codex_apply_patch_for_only_the_handoff(tmp_path):
    """Codex exposes freeform apply_patch input as tool_input.command."""
    store = setup_store(tmp_path, 73.5)
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: AGENT_HANDOFF.md\n"
                "+# Agent Handoff\n"
                "*** End Patch"
            )
        },
    }

    allowed = pre_tool(payload, store)

    assert allowed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_codex_apply_patch_handoff_stops_after_the_file_exists(tmp_path):
    store = setup_store(tmp_path, 73.5)
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: AGENT_HANDOFF.md\n"
                "+# Agent Handoff\n"
                "*** End Patch"
            )
        },
        "tool_response": {"success": True},
    }
    pre_tool(payload, store)
    (tmp_path / "AGENT_HANDOFF.md").write_text("# Agent Handoff\n", encoding="utf-8")

    result = post_tool(payload, store)

    assert store.load_session().state == LandingState.STOPPED
    assert "AGENT_HANDOFF.md" in result["hookSpecificOutput"]["additionalContext"]


def test_land_denies_apply_patch_that_bundles_handoff_with_other_work(tmp_path):
    store = setup_store(tmp_path, 73.5)
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: AGENT_HANDOFF.md\n"
                "+# Agent Handoff\n"
                "*** Add File: src/sneak.py\n"
                "+print('still working')\n"
                "*** End Patch"
            )
        },
    }

    blocked = pre_tool(payload, store)

    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_land_denies_deleting_or_moving_the_handoff(tmp_path):
    store = setup_store(tmp_path, 73.5)
    patches = (
        "*** Begin Patch\n*** Delete File: AGENT_HANDOFF.md\n*** End Patch",
        (
            "*** Begin Patch\n"
            "*** Update File: AGENT_HANDOFF.md\n"
            "*** Move to: elsewhere.md\n"
            "@@\n-old\n+new\n"
            "*** End Patch"
        ),
    )

    for command in patches:
        blocked = pre_tool(
            {"tool_name": "apply_patch", "tool_input": {"command": command}}, store
        )
        assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_land_denies_same_handoff_basename_outside_reserved_path(tmp_path):
    store = setup_store(tmp_path, 73.5)

    blocked = pre_tool(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "other/AGENT_HANDOFF.md"},
        },
        store,
    )

    assert blocked["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_post_handoff_stops_session(tmp_path):
    store = setup_store(tmp_path, 73.5)
    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    (tmp_path / "AGENT_HANDOFF.md").write_text("# handoff", encoding="utf-8")
    result = post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    assert store.load_session().state == LandingState.STOPPED
    assert "AGENT_HANDOFF.md" in result["hookSpecificOutput"]["additionalContext"]


def test_a_handoff_write_that_produced_no_file_does_not_land_the_task(tmp_path):
    """`landed` is terminal and not renewable, so it must not be recorded on a failed write.

    Trusting the write tool's own report would end the budget with no handoff at
    all -- the exact outcome the landing reserve exists to prevent.
    """
    store = setup_store(tmp_path, 73.5)
    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)

    session = store.load_session()
    assert session.state == LandingState.LAND, "the guard stays up until the handoff exists"
    assert session.stop_reason is None


def test_a_handoff_write_the_host_reported_as_failed_does_not_land_the_task(tmp_path):
    store = setup_store(tmp_path, 73.5)
    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    (tmp_path / "AGENT_HANDOFF.md").write_text("# stale from an earlier run", encoding="utf-8")
    post_tool(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "AGENT_HANDOFF.md"},
            "tool_response": {"success": False, "error": "EACCES"},
        },
        store,
    )
    assert store.load_session().state == LandingState.LAND


def test_an_empty_handoff_does_not_land_the_task(tmp_path):
    store = setup_store(tmp_path, 73.5)
    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    (tmp_path / "AGENT_HANDOFF.md").write_text("", encoding="utf-8")
    post_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    assert store.load_session().state == LandingState.LAND


def test_emergency_writes_fallback(tmp_path):
    store = setup_store(tmp_path, 70.4)
    result = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (tmp_path / "AGENT_HANDOFF.md").exists()
    assert store.load_session().state == LandingState.STOPPED


def test_emergency_preserves_an_existing_handoff_and_writes_a_new_task_file(tmp_path):
    old = tmp_path / "AGENT_HANDOFF.md"
    old.write_text("# Previous task\nDo not overwrite me.\n", encoding="utf-8")
    store = setup_store(tmp_path, 70.4)

    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)

    assert old.read_text(encoding="utf-8") == "# Previous task\nDo not overwrite me.\n"
    new_path = tmp_path / "AGENT_HANDOFF-2.md"
    assert new_path.exists()
    assert "implement auth" in new_path.read_text(encoding="utf-8")
    assert store.load_session().handoff_path == "AGENT_HANDOFF-2.md"


def test_land_updates_the_path_reserved_for_the_current_task(tmp_path):
    (tmp_path / "AGENT_HANDOFF.md").write_text("# Previous task", encoding="utf-8")
    store = setup_store(tmp_path, 73.5)

    pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/new.py"}}, store)
    session = store.load_session()
    assert session.handoff_path == "AGENT_HANDOFF-2.md"

    old_path = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF.md"}}, store)
    assert old_path["hookSpecificOutput"]["permissionDecision"] == "deny"
    current_path = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "AGENT_HANDOFF-2.md"}}, store)
    assert current_path.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_task_complete_stops_even_far_above_floor(tmp_path):
    store = setup_store(tmp_path, 91.0)
    session = store.load_session()
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    session.completed_at_remaining_percent = 91.0
    store.save_session(session)
    result = pre_tool({"tool_name": "Write", "tool_input": {"file_path": "src/waste.py"}}, store)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "stopped" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()
