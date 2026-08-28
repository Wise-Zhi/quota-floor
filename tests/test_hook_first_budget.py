"""Opening the budget in the hook, before the model is given the turn.

Leaving it to the model meant loading a skill and spending a tool call on
`landing invoke`. Codex requires a progress message before a tool call, so every
budgeted task opened with a paragraph about Landing instead of the work. That
preamble is structural -- caused by there being a tool call at all -- so no
amount of skill wording removes it, and several rounds of trying proved it.

It also closes a hole: model-mediated triggering could simply fail to trigger,
leaving the user unguarded while believing otherwise.
"""

from pathlib import Path
from time import time

from agent_landing.hooks import user_prompt
from agent_landing.invocation import extract_invocation
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore


def seed(tmp_path: Path, remaining: float = 90.0, provider: str = "claude") -> StateStore:
    store = StateStore(tmp_path)
    store.save_snapshot(QuotaSnapshot(
        provider=provider,
        windows=[QuotaWindow(id="five_hour", used_percent=100 - remaining)],
        sampled_at=time(),
    ))
    return store


def submit(store: StateStore, prompt: str, provider: str = "claude") -> dict:
    return user_prompt({"prompt": prompt, "cwd": str(store.root)}, store, provider=provider)


def context_of(result: dict) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def test_the_budget_exists_before_the_model_is_asked_anything(tmp_path):
    store = seed(tmp_path)
    result = submit(store, "/landing spend 20 refactor auth")

    session = store.load_session()
    assert session is not None, "the hook, not the model, opens the budget"
    assert session.task == "refactor auth"
    assert session.state == LandingState.NORMAL
    # Nothing is asked of the model except to get on with the work.
    assert "do not run any landing command" in context_of(result)


def test_task_text_before_the_marker_is_kept(tmp_path):
    """People write "继续/landing spend 20". This used to be a prompt-level fix."""
    store = seed(tmp_path)
    submit(store, "继续/landing  spend 20")
    assert store.load_session().task == "继续"


def test_the_codex_marker_works_too(tmp_path):
    store = seed(tmp_path, provider="codex")
    submit(store, "$landing leave 70 fix the flaky test", provider="codex")
    session = store.load_session()
    assert session.floor_percent == 70
    assert session.task == "fix the flaky test"


def test_prose_about_the_command_is_left_alone(tmp_path):
    """Blocking here would make Landing impossible to talk about."""
    store = seed(tmp_path)
    for prompt in (
        "why does /landing cancel not work?",
        "explain what /landing spend means",
        "add a test for the /landing parser",
    ):
        assert submit(store, prompt) == {}, prompt
        assert store.load_session() is None, prompt


def test_a_typo_in_an_explicit_invocation_blocks_the_prompt(tmp_path):
    """Silently not guarding is the failure this project ranks worst.

    Blocking means the model never runs, so it cannot start unguarded work and
    cannot narrate an explanation either.
    """
    store = seed(tmp_path)
    result = submit(store, "/landing leave abc do the thing")

    assert result["decision"] == "block"
    assert "Could not start a quota budget" in result["reason"]
    assert store.load_session() is None


def test_a_mid_message_typo_is_prose_not_a_refusal(tmp_path):
    """The one case left ambiguous, resolved towards not breaking conversation."""
    store = seed(tmp_path)
    assert submit(store, "I tried saying /landing leave abc and it failed") == {}
    assert store.load_session() is None


def test_missing_quota_blocks_rather_than_starting_unguarded(tmp_path):
    store = StateStore(tmp_path)  # no snapshot at all
    result = submit(store, "/landing spend 20 do the work")

    assert result["decision"] == "block"
    assert "nothing to budget against" in result["reason"]
    assert "the task was not run" in result["reason"]
    assert store.load_session() is None


def test_a_refused_budget_blocks_and_leaves_the_old_one_standing(tmp_path):
    """The ratchet still applies when the hook is the one opening the budget."""
    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")

    result = submit(store, "/landing leave 5 sneak past the floor")
    assert result["decision"] == "block"
    assert "Refusing to lower it" in result["reason"]
    assert store.load_session().floor_percent == 70


def test_cancel_is_still_handled_by_the_cancel_path(tmp_path):
    store = seed(tmp_path)
    submit(store, "/landing leave 70 a task")
    submit(store, "/landing cancel")
    assert store.load_session().is_cancelled


def test_an_ordinary_message_is_untouched(tmp_path):
    store = seed(tmp_path)
    assert submit(store, "please refactor the auth module") == {}
    assert store.load_session() is None


def test_extract_marks_only_a_leading_marker_authoritative():
    assert extract_invocation("/landing spend 20 x") == ("spend 20 x", True)
    assert extract_invocation("继续/landing spend 20") == ("继续 spend 20", False)
    assert extract_invocation("$LANDING leave 70 x") == ("leave 70 x", True)
    assert extract_invocation("no invocation here") is None


def test_a_path_that_merely_contains_the_word_is_not_an_invocation():
    """`~/.agents/skills/landing/` is documentation, not a budget request."""
    for text in (
        "the skill lives in ~/.agents/skills/landing/",
        "see .claude/skills/landing/SKILL.md",
    ):
        assert extract_invocation(text) is None, text


def test_a_blocked_invocation_leaves_a_readable_reason(tmp_path):
    """Codex renders a blocked prompt as the single word "Blocked".

    Measured on codex-cli 0.150.0-alpha.8: neither `reason`, `stopReason`,
    `systemMessage`, nor the hook's stderr reaches the user. Blocking is still
    right -- the alternative is running the turn and trusting the model not to
    work unguarded -- but a refusal nobody can read is a dead end, so it is kept
    for `landing status`.
    """
    store = seed(tmp_path)
    assert submit(store, "/landing leave abc do the thing")["decision"] == "block"

    kept = store.load_refusal()
    assert kept is not None
    assert "no quota percentage was given" in kept


def test_opening_a_budget_clears_the_previous_refusal(tmp_path):
    """A stale explanation is its own confusion once the budget exists."""
    store = seed(tmp_path)
    submit(store, "/landing leave abc do the thing")
    assert store.load_refusal() is not None

    submit(store, "/landing leave 70 do the thing")
    assert store.load_refusal() is None


def test_a_refused_budget_is_also_kept(tmp_path):
    """Not only parse errors: the ratchet refusal is just as invisible."""
    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")
    submit(store, "/landing leave 5 lower the floor")
    assert "Refusing to lower it" in (store.load_refusal() or "")


def test_prose_never_records_a_refusal(tmp_path):
    """Discussing the command must not leave an error behind for the user."""
    store = seed(tmp_path)
    submit(store, "why does /landing cancel not work?")
    assert store.load_refusal() is None
