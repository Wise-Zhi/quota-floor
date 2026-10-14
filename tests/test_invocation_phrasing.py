"""Natural phrasing around the settings, without loosening what the settings mean.

The skill advertised "leave me 70% quota" and "spend at most 15% on this", and
the parser refused every one of them -- a budget request that cost the user a
whole turn to learn the real syntax. Filler is now accepted *around* the two
safety-critical fields, but never in place of them: `leave`/`spend` is still
mandatory, and a qualifier that contradicts the keyword is refused, not guessed.
"""

import pytest

from agent_landing.invocation import SYNTAX, extract_invocation, parse_invocation


@pytest.mark.parametrize("raw, mode, percent, window, task", [
    ("leave me 70% quota and refactor auth", "preserve", 70.0, "auto", "and refactor auth"),
    ("leave at least 70 fix the tests", "preserve", 70.0, "auto", "fix the tests"),
    ("leave me at least 70% of my quota", "preserve", 70.0, "auto", ""),
    ("spend at most 15% on this", "max_spend", 15.0, "auto", "on this"),
    ("spend up to 15 fix the tests", "max_spend", 15.0, "auto", "fix the tests"),
    ("spend no more than 15% fix it", "max_spend", 15.0, "auto", "fix it"),
    ("spend only 10 fix it", "max_spend", 10.0, "auto", "fix it"),
    ("spend at most weekly 20 x", "max_spend", 20.0, "seven_day", "x"),
    ("weekly leave at least 70 x", "preserve", 70.0, "seven_day", "x"),
    # The same phrasing written after the task.
    ("fix the tests, spend at most 20%", "max_spend", 20.0, "auto", "fix the tests,"),
    ("refactor auth leave me 70%", "preserve", 70.0, "auto", "refactor auth"),
    # A trailing comma is punctuation, not part of the number.
    ("leave 70, fix the tests", "preserve", 70.0, "auto", "fix the tests"),
])
def test_english_filler_around_the_settings_is_accepted(raw, mode, percent, window, task):
    parsed = parse_invocation(raw)
    assert parsed.ok, parsed.error
    assert (parsed.mode, parsed.percent, parsed.window, parsed.task) == (mode, percent, window, task)


@pytest.mark.parametrize("raw, mode, percent, window, task", [
    ("留 70 修测试", "preserve", 70.0, "auto", "修测试"),
    ("留70 修测试", "preserve", 70.0, "auto", "修测试"),
    ("留70%修测试", "preserve", 70.0, "auto", "修测试"),
    ("保留 70% 重构登录", "preserve", 70.0, "auto", "重构登录"),
    ("至少留70%", "preserve", 70.0, "auto", ""),
    ("留 5小时 30 查一下", "preserve", 30.0, "five_hour", "查一下"),
    ("花 20 修测试", "max_spend", 20.0, "auto", "修测试"),
    ("用 20 修测试", "max_spend", 20.0, "auto", "修测试"),
    ("最多花20%", "max_spend", 20.0, "auto", ""),
    ("花最多 20% 修测试", "max_spend", 20.0, "auto", "修测试"),
    ("修测试 花20", "max_spend", 20.0, "auto", "修测试"),
])
def test_chinese_keywords_follow_the_same_rules(raw, mode, percent, window, task):
    parsed = parse_invocation(raw)
    assert parsed.ok, parsed.error
    assert (parsed.mode, parsed.percent, parsed.window, parsed.task) == (mode, percent, window, task)


def test_text_before_the_marker_still_joins_a_chinese_invocation():
    arguments, _ = extract_invocation("继续/landing 花20")
    parsed = parse_invocation(arguments)
    assert parsed.ok, parsed.error
    assert (parsed.mode, parsed.percent, parsed.task) == ("max_spend", 20.0, "继续")


@pytest.mark.parametrize("raw", [
    "leave at most 30 x",
    "leave no more than 30 x",
    "spend at least 15 x",
    "最多留 30",
    "至少花 15",
])
def test_a_qualifier_that_contradicts_the_keyword_is_refused(raw):
    """`leave at most 30` has no sound reading as a floor; guessing one is the
    exact failure the mandatory keyword exists to prevent."""
    parsed = parse_invocation(raw)
    assert not parsed.ok
    assert "contradict" in parsed.error


@pytest.mark.parametrize("raw", [
    "70 修测试",
    "最多 20 修测试",
    "at most 15% on this",
    "stop before my weekly quota drops below 40%",
    "leave me alone",
])
def test_filler_never_stands_in_for_the_keyword(raw):
    assert not parse_invocation(raw).ok


def test_a_number_glued_to_task_text_is_not_split_into_a_budget():
    """`用3个线程` is task text that happens to start with a spend keyword. Only
    an explicit `%` or a space makes the number part of the settings."""
    assert not parse_invocation("用3个线程跑测试").ok


@pytest.mark.parametrize("raw, task", [
    ("leave 70 use pytest to check", "use pytest to check"),
    ("留 70 用 pytest 跑测试", "用 pytest 跑测试"),
    ("spend 20 keep the old API", "keep the old API"),
])
def test_a_keyword_alias_starting_the_task_is_task_text(raw, task):
    """`use` and `用` are spend aliases, and also how half of all tasks begin.
    After a complete group, an alias not followed by a setting starts the task."""
    parsed = parse_invocation(raw)
    assert parsed.ok, parsed.error
    assert parsed.task == task


def test_two_complete_groups_are_still_refused():
    """The alias rule must not let a second budget slip into the task text."""
    assert not parse_invocation("leave 70 spend 20 x").ok
    assert not parse_invocation("留 70 花 20 x").ok


def test_public_syntax_is_the_short_form():
    """The window token still works but is not part of what autocomplete teaches."""
    assert SYNTAX == "/landing leave|spend <percent> [task]"
    assert parse_invocation("leave weekly 70 x").window == "seven_day"


def test_skill_argument_hint_matches_the_public_syntax():
    from pathlib import Path

    skill = Path("src/agent_landing/assets/claude/landing/SKILL.md").read_text(encoding="utf-8")
    assert f"argument-hint: '{SYNTAX.removeprefix('/landing ')}'" in skill


def test_invoke_accepts_the_message_with_its_marker_still_on(tmp_path, monkeypatch, capsys):
    """The skill says to strip `/landing`; a model that forgets must not turn a
    valid request into a refusal."""
    import sys
    from time import time

    from agent_landing.cli import main
    from agent_landing.models import QuotaSnapshot, QuotaWindow
    from agent_landing.state import StateStore

    monkeypatch.setenv("LANDING_HOME", str(tmp_path))
    StateStore(tmp_path).save_snapshot(QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="seven_day", used_percent=10)],
        sampled_at=time(),
    ))
    monkeypatch.setattr(sys, "argv", ["landing", "invoke", "/landing leave 70 fix it", "--provider", "claude"])
    assert main() == 0, capsys.readouterr().err
    session = StateStore(tmp_path).load_session()
    assert (session.task, session.mode, session.floor_percent) == ("fix it", "preserve", 70.0)
