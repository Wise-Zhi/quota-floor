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

import pytest

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


def test_the_user_may_lower_their_own_floor_from_the_chat(tmp_path):
    """The ratchet keys on the channel, not the direction.

    An agent can run `landing start`; it cannot manufacture a UserPromptSubmit.
    So a lower floor arriving here is the user's own, and refusing it while
    allowing `/landing cancel` from the same message was incoherent -- it
    blocked the milder act, permitted the total one, and sent the user through
    a completely unguarded state to reach the smaller change.
    """
    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")

    result = submit(store, "/landing leave 50 second task")
    assert "decision" not in result
    assert store.load_session().floor_percent == 50


def test_lowering_a_floor_says_so_before_anything_else(tmp_path):
    """The one way this goes wrong is a lower number written in passing.

    A floor tucked into a message about something else would otherwise replace
    a stricter one the user had forgotten was running, silently.
    """
    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")

    context = context_of(submit(store, "/landing leave 50 second task"))
    assert "LOWERED from 70.0% to 50.0%" in context


def test_an_agent_still_cannot_lower_the_floor(tmp_path):
    """The guarantee that survives: only the user's own channel may weaken it."""
    from agent_landing.budget import BudgetRefused, start_budget

    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")
    snapshot = store.load_snapshot()

    with pytest.raises(BudgetRefused) as excinfo:
        start_budget(store, snapshot, provider="claude", task="agent task", preserve=5.0)
    assert "Refusing to lower it" in str(excinfo.value)
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
    submit(store, "/landing 70 fix the tests")
    assert "leave or quota to spend" in (store.load_refusal() or "")


def test_prose_never_records_a_refusal(tmp_path):
    """Discussing the command must not leave an error behind for the user."""
    store = seed(tmp_path)
    submit(store, "why does /landing cancel not work?")
    assert store.load_refusal() is None


def test_a_marker_less_budget_request_is_not_silently_unguarded(tmp_path):
    """Codex ships no skill now, so plain language reaches nothing at all.

    A hint cannot open the budget -- the percentage would be a guess, and a
    guessed floor is not the user's floor -- but it can stop the work from
    running unguarded while the user believes otherwise.
    """
    store = seed(tmp_path)
    context = context_of(submit(store, "leave me 70% quota and refactor auth"))

    assert "NO budget was opened" in context
    assert "/landing leave 70" in context
    assert store.load_session() is None, "a hint must never open a budget"


def test_the_hint_needs_both_a_quota_word_and_a_percentage(tmp_path):
    """Either alone is ordinary conversation and must not trigger anything."""
    store = seed(tmp_path)
    for prompt in ("70% of the tests pass", "the quota endpoint is slow", "refactor auth"):
        assert submit(store, prompt) == {}, prompt


def test_a_real_invocation_never_gets_the_hint(tmp_path):
    store = seed(tmp_path)
    context = context_of(submit(store, "/landing leave 70 refactor auth"))
    assert "NO budget was opened" not in context
    assert store.load_session().floor_percent == 70


def test_no_hint_while_a_budget_is_already_guarding(tmp_path):
    """Nothing to warn about once the floor is in force."""
    store = seed(tmp_path)
    submit(store, "/landing leave 70 first task")
    assert submit(store, "now leave me 50% quota instead") == {}


def test_an_interpreter_path_with_spaces_is_quoted(monkeypatch):
    """An unquoted `C:/Program Files/...` would be two arguments, not a command."""
    import sys

    from agent_landing.hooks import landing_command

    monkeypatch.setattr(sys, "executable", "C:/Program Files/py/python.exe")
    assert landing_command("complete").startswith('"C:/Program Files/py/python.exe"')


SKILL = Path(__file__).resolve().parents[1] / "src" / "agent_landing" / "assets" / "claude" / "landing" / "SKILL.md"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_the_model_is_told_to_do_the_most_valuable_part_first(tmp_path, provider):
    """LAND can interrupt at any point, so the finished part should be the best part.

    Codex ships no skill, so the hook is its only copy. The rule changes the order
    of the work, never its amount: an agent told to estimate what fits would treat
    the floor as a spending target or trim the task before any quota had moved.
    """
    store = seed(tmp_path, provider=provider)
    marker = "$landing" if provider == "codex" else "/landing"
    context = context_of(submit(store, f"{marker} leave 70 fix the tests", provider=provider))
    skill = SKILL.read_text(encoding="utf-8")

    for text in (context, " ".join(skill.split())):
        assert "most important part of the task that is useful on its own first" in text
        assert "never its amount" in text
        assert "do not estimate how much fits in the budget" in text
