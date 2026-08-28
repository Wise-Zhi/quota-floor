"""The handoff is what the user is left holding when the budget runs out.

Every other part of Landing exists to make sure this file gets written while
there is still quota to write it with, so it had no direct coverage at all --
the three functions here were only ever exercised incidentally, through the gate.
"""

from pathlib import Path

from agent_landing.handoff import (
    NO_TASK_RECORDED,
    landing_prompt,
    reserve_handoff_path,
    write_fallback_handoff,
)
from agent_landing.models import LandingState, PolicyDecision, TaskSession

REQUIRED_SECTIONS = (
    "Original task",
    "completed and verified",
    "Files changed",
    "Tests/validation",
    "incomplete or unverified",
    "Failed approaches",
    "next steps",
    "resume prompt",
)


def session_at(tmp_path: Path, task: str = "implement auth") -> TaskSession:
    return TaskSession(
        task=task,
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=95,
        cwd=str(tmp_path),
    )


def decision_at(remaining: float = 73.5, state: LandingState = LandingState.LAND) -> PolicyDecision:
    return PolicyDecision(
        state=state,
        remaining_percent=remaining,
        floor_percent=70.0,
        reserve_percent=4.0,
        development_floor_percent=74.0,
        reason="test",
    )


# --- claiming a path ----------------------------------------------------------

def test_the_first_handoff_takes_the_plain_name(tmp_path):
    session = session_at(tmp_path)
    assert reserve_handoff_path(session) == tmp_path / "AGENT_HANDOFF.md"


def test_an_earlier_task_s_handoff_is_never_overwritten(tmp_path):
    """Landing twice in one repository must not destroy the first handoff."""
    (tmp_path / "AGENT_HANDOFF.md").write_text("first task's work", encoding="utf-8")
    session = session_at(tmp_path)

    assert reserve_handoff_path(session) == tmp_path / "AGENT_HANDOFF-2.md"
    assert (tmp_path / "AGENT_HANDOFF.md").read_text(encoding="utf-8") == "first task's work"


def test_it_keeps_counting_past_the_second(tmp_path):
    for name in ("AGENT_HANDOFF.md", "AGENT_HANDOFF-2.md", "AGENT_HANDOFF-3.md"):
        (tmp_path / name).write_text("taken", encoding="utf-8")
    assert reserve_handoff_path(session_at(tmp_path)).name == "AGENT_HANDOFF-4.md"


def test_the_claim_is_remembered_so_the_task_keeps_one_file(tmp_path):
    """LAND may write, then rewrite. The second write must not skip to -2."""
    session = session_at(tmp_path)
    first = reserve_handoff_path(session)
    first.write_text("draft", encoding="utf-8")

    assert reserve_handoff_path(session) == first
    assert session.handoff_path_reserved is True


def test_the_reserved_name_is_recorded_on_the_session(tmp_path):
    """The gate allows a write by matching this name, so it has to be stored."""
    (tmp_path / "AGENT_HANDOFF.md").write_text("taken", encoding="utf-8")
    session = session_at(tmp_path)
    reserve_handoff_path(session)
    assert session.handoff_path == "AGENT_HANDOFF-2.md"


def test_an_absolute_handoff_path_stays_absolute(tmp_path):
    session = session_at(tmp_path)
    session.handoff_path = str(tmp_path / "out" / "HANDOFF.md")
    (tmp_path / "out").mkdir()

    claimed = reserve_handoff_path(session)
    assert claimed.is_absolute()
    assert Path(session.handoff_path).is_absolute()


# --- what the model is told to write -------------------------------------------

def test_the_landing_prompt_asks_for_every_required_section(tmp_path):
    """A handoff missing next steps or a resume prompt cannot be resumed from."""
    prompt = landing_prompt(session_at(tmp_path), decision_at())
    for section in REQUIRED_SECTIONS:
        assert section.lower() in prompt.lower(), section


def test_the_landing_prompt_names_the_reserved_file_and_the_numbers(tmp_path):
    session = session_at(tmp_path)
    session.handoff_path = "AGENT_HANDOFF-2.md"
    prompt = landing_prompt(session, decision_at(remaining=73.5))

    assert "AGENT_HANDOFF-2.md" in prompt
    assert "70.0%" in prompt and "73.5%" in prompt
    # It must not invite unrelated work while landing.
    assert "Do not start new implementation work" in prompt


# --- the deterministic fallback -------------------------------------------------

def test_the_fallback_records_enough_to_resume_without_a_model_turn(tmp_path):
    """EMERGENCY has no model turn left, so this is written from local state."""
    session = session_at(tmp_path)
    (tmp_path / "changed.py").write_text("x = 1", encoding="utf-8")

    path = write_fallback_handoff(session, decision_at(state=LandingState.EMERGENCY))
    body = path.read_text(encoding="utf-8")

    assert path == tmp_path / "AGENT_HANDOFF.md"
    assert "implement auth" in body
    assert "emergency" in body
    assert "Resume Prompt" in body
    for heading in ("Original Task", "Why Work Stopped", "Repository State", "Next Steps"):
        assert f"## {heading}" in body, heading


def test_the_fallback_never_claims_work_it_cannot_verify(tmp_path):
    """It knows the diff, not whether the diff is correct. Saying otherwise is worse
    than saying nothing: the next session would skip work that was never done."""
    body = write_fallback_handoff(session_at(tmp_path), decision_at()).read_text(encoding="utf-8")
    assert "Unknown to the deterministic fallback" in body
    assert "Do not assume changed files are correct or tested" in body


def test_the_fallback_says_so_when_no_task_was_named(tmp_path):
    """A budget can now attach to work already under way, leaving no label."""
    body = write_fallback_handoff(session_at(tmp_path, task=""), decision_at()).read_text(
        encoding="utf-8"
    )
    assert NO_TASK_RECORDED in body
    assert "## Original Task\n\n" not in body, "an empty heading tells the next session nothing"


def test_the_fallback_respects_an_earlier_handoff(tmp_path):
    (tmp_path / "AGENT_HANDOFF.md").write_text("first task", encoding="utf-8")
    path = write_fallback_handoff(session_at(tmp_path), decision_at())

    assert path.name == "AGENT_HANDOFF-2.md"
    assert (tmp_path / "AGENT_HANDOFF.md").read_text(encoding="utf-8") == "first task"


def test_the_fallback_still_writes_outside_a_git_repository(tmp_path):
    """Quota does not run out only in tidy checkouts."""
    body = write_fallback_handoff(session_at(tmp_path), decision_at()).read_text(encoding="utf-8")
    assert "## Repository State" in body
    assert len(body) > 500
