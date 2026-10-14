"""The handoff is what the user is left holding when the budget runs out.

It lives under Landing's own state directory, never in the user's folder: a
budget is not always attached to a project, and when it is, a pile of
`AGENT_HANDOFF-N.md` files is clutter the user did not ask for. The model no
longer writes a file at all -- its final reply *is* the handoff, and the Stop
hook saves it (see test_stop_handoff).
"""

from pathlib import Path

from agent_landing.handoff import (
    NO_TASK_RECORDED,
    landing_prompt,
    new_handoff_path,
    save_reply_handoff,
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
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return TaskSession(
        task=task,
        provider="claude",
        window="seven_day",
        floor_percent=70,
        started_remaining_percent=95,
        cwd=str(project),
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


# --- where it goes --------------------------------------------------------------

def test_a_handoff_path_is_outside_the_working_folder(tmp_path):
    session = session_at(tmp_path)
    directory = tmp_path / "state" / "handoffs"
    path = new_handoff_path(directory, session)

    assert path.parent == directory
    assert Path(session.cwd) not in path.parents
    assert path.suffix == ".md"
    assert "implement-auth" in path.name


def test_two_handoffs_never_share_a_file(tmp_path):
    session = session_at(tmp_path)
    directory = tmp_path / "handoffs"
    directory.mkdir()
    first = new_handoff_path(directory, session)
    first.write_text("first", encoding="utf-8")

    second = new_handoff_path(directory, session)
    assert second != first
    assert first.read_text(encoding="utf-8") == "first"


def test_a_task_name_cannot_escape_the_directory(tmp_path):
    session = session_at(tmp_path, task="../../etc/passwd and more")
    path = new_handoff_path(tmp_path / "handoffs", session)
    assert path.parent == tmp_path / "handoffs"


def test_a_chinese_task_name_is_kept_readable(tmp_path):
    path = new_handoff_path(tmp_path / "handoffs", session_at(tmp_path, task="修复登录测试"))
    assert "修复登录测试" in path.name


def test_no_task_still_gets_a_name(tmp_path):
    path = new_handoff_path(tmp_path / "handoffs", session_at(tmp_path, task=""))
    assert path.name.endswith("-handoff.md")


# --- what the model is told -------------------------------------------------------

def test_the_landing_prompt_asks_for_every_required_section(tmp_path):
    """A handoff missing next steps or a resume prompt cannot be resumed from."""
    prompt = landing_prompt(session_at(tmp_path), decision_at())
    for section in REQUIRED_SECTIONS:
        assert section.lower() in prompt.lower(), section


def test_the_landing_prompt_asks_for_a_reply_not_a_file(tmp_path):
    prompt = landing_prompt(session_at(tmp_path), decision_at(remaining=73.5))

    assert "final reply" in prompt
    assert "Do not write a handoff file" in prompt
    assert "AGENT_HANDOFF" not in prompt
    assert "70.0%" in prompt and "73.5%" in prompt
    assert "Do not start new implementation work" in prompt


# --- saving the model's reply -------------------------------------------------------

def test_the_reply_is_saved_with_its_context(tmp_path):
    session = session_at(tmp_path)
    path = save_reply_handoff(session, "## Completed\n- auth model", tmp_path / "handoffs")
    body = path.read_text(encoding="utf-8")

    assert "## Completed\n- auth model" in body
    assert "implement auth" in body
    assert session.cwd in body, "the next session needs to know which folder this was"


# --- the deterministic fallback -------------------------------------------------

def test_the_fallback_records_enough_to_resume_without_a_model_turn(tmp_path):
    """EMERGENCY has no model turn left, so this is written from local state."""
    session = session_at(tmp_path)
    path = write_fallback_handoff(
        session, decision_at(state=LandingState.EMERGENCY), tmp_path / "handoffs"
    )
    body = path.read_text(encoding="utf-8")

    assert path.parent == tmp_path / "handoffs"
    assert not list(Path(session.cwd).iterdir()), "nothing is written into the user's folder"
    assert "implement auth" in body
    assert "emergency" in body
    assert "Resume Prompt" in body
    for heading in ("Original Task", "Why Work Stopped", "Working Folder", "Next Steps"):
        assert f"## {heading}" in body, heading


def test_the_fallback_never_claims_work_it_cannot_verify(tmp_path):
    """It knows the diff, not whether the diff is correct. Saying otherwise is worse
    than saying nothing: the next session would skip work that was never done."""
    body = write_fallback_handoff(
        session_at(tmp_path), decision_at(), tmp_path / "handoffs"
    ).read_text(encoding="utf-8")
    assert "Unknown to the deterministic fallback" in body
    assert "Do not assume changed files are correct or tested" in body


def test_the_fallback_says_so_when_no_task_was_named(tmp_path):
    body = write_fallback_handoff(
        session_at(tmp_path, task=""), decision_at(), tmp_path / "handoffs"
    ).read_text(encoding="utf-8")
    assert NO_TASK_RECORDED in body
    assert "## Original Task\n\n" not in body, "an empty heading tells the next session nothing"


def test_the_fallback_outside_a_git_repository_says_so_instead_of_dumping_errors(tmp_path):
    """A budget is not always attached to a project."""
    body = write_fallback_handoff(
        session_at(tmp_path), decision_at(), tmp_path / "handoffs"
    ).read_text(encoding="utf-8")
    assert "not a git repository" in body
    assert "fatal:" not in body
