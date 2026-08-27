"""Invocation parsing is deterministic, identical across hosts, and refuses typos."""

import pytest

from agent_landing.invocation import EXAMPLES, SYNTAX, parse_invocation


def test_public_syntax_does_not_teach_the_optional_double_dash():
    """The task separator is accepted for compatibility, not taught to users."""
    assert "--" not in SYNTAX
    assert all("--" not in example for example in EXAMPLES)


@pytest.mark.parametrize("raw, window, mode, percent, task", [
    ("leave 70 -- refactor auth", "auto", "preserve", 70.0, "refactor auth"),
    ("spend 15 -- fix the tests", "auto", "max_spend", 15.0, "fix the tests"),
    ("keep 70 -- x", "auto", "preserve", 70.0, "x"),
    ("use 15 -- x", "auto", "max_spend", 15.0, "x"),
    ("max 15 -- x", "auto", "max_spend", 15.0, "x"),
    ("leave weekly 75 -- fix the checkout tests", "seven_day", "preserve", 75.0,
     "fix the checkout tests"),
    ("leave 5h 30 -- investigate", "five_hour", "preserve", 30.0, "investigate"),
    ("spend five_hour 10 -- x", "five_hour", "max_spend", 10.0, "x"),
    ("leave w 70 -- x", "seven_day", "preserve", 70.0, "x"),
    ("leave 70% -- x", "auto", "preserve", 70.0, "x"),
    ("leave 70.5 -- x", "auto", "preserve", 70.5, "x"),
    # Option tokens may appear in any order before the task.
    ("30 5h leave -- x", "five_hour", "preserve", 30.0, "x"),
    ("weekly spend 15 -- x", "seven_day", "max_spend", 15.0, "x"),
    # `--` is optional: the first token that is not an option starts the task.
    ("leave 70 fix the checkout tests", "auto", "preserve", 70.0, "fix the checkout tests"),
    ("spend 15 fix the tests", "auto", "max_spend", 15.0, "fix the tests"),
    # Option words appearing *inside* the task text are not reinterpreted.
    ("leave 70 -- spend less time on this", "auto", "preserve", 70.0, "spend less time on this"),
    ("leave 70 -- 5h is mentioned in the task", "auto", "preserve", 70.0,
     "5h is mentioned in the task"),
])
def test_valid_invocations(raw, window, mode, percent, task):
    parsed = parse_invocation(raw)
    assert parsed.ok, parsed.error
    assert (parsed.window, parsed.mode, parsed.percent, parsed.task) == (window, mode, percent, task)


def test_omitted_window_defaults_to_auto_not_a_hardcoded_plan():
    """A Plus account has no weekly window; hardcoding one would break it."""
    assert parse_invocation("leave 70 -- x").window == "auto"


def test_five_hour_token_is_never_read_as_a_percentage_of_five():
    """The old syntax needed a written warning against exactly this misparse."""
    parsed = parse_invocation("leave 5h 30 -- x")
    assert parsed.percent == 30.0
    assert parsed.window == "five_hour"


# --- a bare number is refused, never defaulted --------------------------------

@pytest.mark.parametrize("raw", [
    "70 -- fix the tests",
    "70 fix the tests",
    "weekly 75 -- fix the tests",
    "5h 30 -- x",
    "70",
])
def test_bare_number_is_refused_because_its_meaning_is_ambiguous(raw):
    parsed = parse_invocation(raw)
    assert not parsed.ok
    assert "leave or quota to spend" in parsed.error


def test_bare_number_refusal_offers_both_readings_spelled_out():
    parsed = parse_invocation("70 -- fix the tests")
    assert len(parsed.alternatives) == 2
    joined = "\n".join(parsed.alternatives)
    assert "leave 70" in joined and "spend 70" in joined
    assert "still remains" in joined
    assert "percentage points" in joined
    # The user's own window and task survive into both suggestions.
    assert joined.count("fix the tests") == 2


def test_bare_number_refusal_keeps_an_explicit_window():
    parsed = parse_invocation("weekly 75 -- x")
    assert all("seven_day 75" in alternative for alternative in parsed.alternatives)
    # Whatever it suggests must itself parse, or the refusal sends the user in circles.
    for alternative in parsed.alternatives:
        command = alternative.split("   (")[0].removeprefix("/landing ")
        assert parse_invocation(command).ok, alternative


def test_dropping_the_keyword_cannot_silently_invert_the_meaning():
    """The rule the mandatory keyword exists to enforce.

    With a default, "spend 15" minus its keyword would remain a legal invocation
    meaning "leave 15" -- authorizing 76 points of spend instead of 15.
    """
    explicit = parse_invocation("spend 15 -- x")
    assert explicit.ok and explicit.mode == "max_spend"

    corrupted = parse_invocation("15 -- x")
    assert not corrupted.ok, "a dropped keyword must never yield a legal invocation"


# --- other refusals -----------------------------------------------------------

@pytest.mark.parametrize("raw, fragment", [
    ("", "no arguments"),
    ("-- just a task", "no quota percentage"),
    ("fix the tests", "no quota percentage"),
    ("leave 70", "no task"),
    ("spend 15 --", "no task"),
    ("leave 70 --   ", "no task"),
    ("leave 150 -- x", "between 0 and 100"),
    ("leave 70 80 -- x", "more than one quota percentage"),
    ("leave weekly 5h 70 -- x", "more than one quota window"),
    ("leave spend 70 -- x", "opposite things"),
])
def test_invalid_invocations_are_refused(raw, fragment):
    parsed = parse_invocation(raw)
    assert not parsed.ok
    assert fragment in parsed.error
    assert "/landing" in parsed.message()


def test_refusal_never_silently_starts_a_budget():
    parsed = parse_invocation("leave 7O -- fix the tests")  # letter O, not zero
    assert not parsed.ok
    assert parsed.percent is None


def test_near_miss_offers_a_valid_suggestion_without_applying_it():
    parsed = parse_invocation("leave 7O -- fix the tests")
    assert parsed.suggestion is not None
    assert "leave 70" in parsed.suggestion, "the suggestion must itself be valid syntax"
    assert not parsed.ok, "a suggestion must never be auto-applied"


def test_missing_task_suggestion_keeps_the_users_own_mode():
    assert "spend 15" in parse_invocation("spend 15").suggestion
    assert "leave 70" in parse_invocation("leave 70").suggestion


def test_parser_is_shared_so_both_hosts_agree():
    """`/landing X` and `$landing X` route through this one function."""
    for raw in ("leave 70 -- x", "spend 15 -- y", "leave 5h 30 -- z", "leave 70 no dashes"):
        assert parse_invocation(raw).ok
        assert parse_invocation(raw).to_dict() == parse_invocation(raw).to_dict()


def test_every_example_in_the_docs_actually_parses():
    """Documentation examples are executable claims, so they are checked.

    A refused example is worse than no example: it teaches syntax that the guard
    rejects, and the user learns to distrust the refusal messages.
    """
    import re
    from pathlib import Path

    from agent_landing.invocation import EXAMPLES, extract_invocation

    # The packaged assets are the source of truth; the in-tree copies are pinned
    # to them by test_installation. A missing path would silently skip, so they
    # are asserted present rather than tolerated.
    roots = [
        Path("src/agent_landing/assets/claude/landing/SKILL.md"),
        Path("src/agent_landing/assets/codex/landing/SKILL.md"),
        Path(".claude/skills/landing/SKILL.md"),
        Path(".agents/skills/landing/SKILL.md"),
        Path("README.md"),
    ]
    for path in roots:
        assert path.exists(), f"documentation root disappeared: {path}"
    found = [example.split(" ", 1)[1] for example in EXAMPLES]
    for path in roots:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            # Scrape the whole inline-code span, not just the tail after the
            # marker, and read it the way the hook does. Task text may sit
            # *before* the marker -- `继续/landing spend 20` -- and taking only
            # the tail turned a valid documented example into `spend 20`, which
            # is correctly refused for having no task.
            for span in re.findall(r"`([^`]*[/$]landing[^`]*)`", line):
                extracted = extract_invocation(span)
                if extracted is None:
                    continue
                argument = extracted[0].strip()
                if not argument:
                    continue  # prose naming the marker itself, not an example
                if argument.startswith(("start", "status", "invoke", "complete", "resume")):
                    continue
                found.append(argument)

    assert found, "no examples were discovered; the scraper is broken"
    for argument in found:
        argument = re.sub(r"\s{2,}\(.*$", "", argument)  # drop trailing prose
        if "<" in argument:  # placeholder form, not a concrete example
            continue
        assert parse_invocation(argument).ok, f"documented example is refused: {argument!r}"


# --- settings written after the task ---------------------------------------

@pytest.mark.parametrize("raw, mode, percent, window, task", [
    ("fix the checkout tests leave 70", "preserve", 70.0, "auto", "fix the checkout tests"),
    ("fix the tests spend 15", "max_spend", 15.0, "auto", "fix the tests"),
    ("refactor auth -- leave weekly 70", "preserve", 70.0, "seven_day", "refactor auth"),
    ("investigate the flaky test leave 5h 30", "preserve", 30.0, "five_hour",
     "investigate the flaky test"),
])
def test_settings_may_follow_the_task(raw, mode, percent, window, task):
    """GNU-style tools permute options and operands; both orders are natural."""
    parsed = parse_invocation(raw)
    assert parsed.ok, parsed.error
    assert (parsed.mode, parsed.percent, parsed.window, parsed.task) == (mode, percent, window, task)


def test_trailing_group_still_requires_an_explicit_keyword():
    """A trailing bare number is not a settings group, so it stays task text."""
    parsed = parse_invocation("fix the tests 70")
    assert not parsed.ok
    assert "no quota percentage" in parsed.error


def test_keyword_inside_task_text_is_not_mistaken_for_settings():
    parsed = parse_invocation("leave 70 -- refactor the leave module")
    assert parsed.ok
    assert parsed.task == "refactor the leave module"
    assert parsed.percent == 70.0


def test_number_inside_task_text_is_not_mistaken_for_settings():
    """`70` alone at the end is not a group -- no keyword, so it is task text."""
    parsed = parse_invocation("leave 80 -- bump the timeout to 70")
    assert parsed.ok
    assert parsed.percent == 80.0
    assert parsed.task == "bump the timeout to 70"


def test_leading_settings_win_when_both_ends_look_like_a_group():
    """The documented form stays authoritative."""
    parsed = parse_invocation("leave 70 fix it spend 15")
    assert parsed.ok
    assert (parsed.mode, parsed.percent) == ("preserve", 70.0)
    assert parsed.task == "fix it spend 15"


def test_mode_is_never_inferred_from_trailing_text():
    """The safety-critical fields stay explicit regardless of where they appear."""
    for raw in ("fix the tests 70", "fix the tests weekly 70", "do the thing 5h"):
        assert not parse_invocation(raw).ok
