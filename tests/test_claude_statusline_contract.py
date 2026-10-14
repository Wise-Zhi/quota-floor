"""Pins the parser to Claude Code's documented statusLine payload.

The payload below is the example from the official statusLine documentation,
reproduced verbatim. It is the one external contract Quota Floor depends on for
Claude quota, so a change on either side should fail here loudly rather than show
up as a guard that quietly stops guarding.

Documented constraints this encodes:
  - `rate_limits` appears only for Claude.ai subscriptions, and only after the
    first API response of a session.
  - `five_hour` and `seven_day` may each be absent independently.
  - `used_percentage` is quota *consumed*, 0 to 100.
"""

import json
from pathlib import Path

import pytest

from agent_landing.models import LandingState, TaskSession
from agent_landing.policy import unknown_quota_decision
from agent_landing.providers import parse_claude_statusline
from agent_landing.state import StateStore

DOCUMENTED_PAYLOAD = json.loads("""
{
  "cwd": "/current/working/directory",
  "session_id": "abc123...",
  "session_name": "my-session",
  "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_path": "/path/to/transcript.jsonl",
  "model": {"id": "claude-opus-5", "display_name": "Opus"},
  "workspace": {
    "current_dir": "/current/working/directory",
    "project_dir": "/original/project/directory",
    "added_dirs": [],
    "git_worktree": "feature-xyz",
    "repo": {"host": "github.com", "owner": "anthropics", "name": "claude-code"}
  },
  "version": "2.1.90",
  "output_style": {"name": "default"},
  "cost": {
    "total_cost_usd": 0.01234,
    "total_duration_ms": 45000,
    "total_api_duration_ms": 2300,
    "total_lines_added": 156,
    "total_lines_removed": 23
  },
  "context_window": {
    "total_input_tokens": 15500,
    "total_output_tokens": 1200,
    "context_window_size": 200000,
    "used_percentage": 8,
    "remaining_percentage": 92,
    "current_usage": {
      "input_tokens": 8500,
      "output_tokens": 1200,
      "cache_creation_input_tokens": 5000,
      "cache_read_input_tokens": 2000
    }
  },
  "exceeds_200k_tokens": false,
  "fast_mode": false,
  "effort": {"level": "high"},
  "thinking": {"enabled": true},
  "rate_limits": {
    "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
    "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}
  },
  "vim": {"mode": "NORMAL"},
  "agent": {"name": "security-reviewer"}
}
""")


def test_documented_payload_parses_into_both_windows():
    snapshot = parse_claude_statusline(DOCUMENTED_PAYLOAD)
    assert snapshot.provider == "claude"
    assert {w.id for w in snapshot.windows} == {"five_hour", "seven_day"}
    assert snapshot.unreadable_windows == []


def test_used_percentage_is_consumed_quota_not_remaining():
    """23.5 means 23.5% spent -> 76.5% left. Inverting this inverts the guard."""
    snapshot = parse_claude_statusline(DOCUMENTED_PAYLOAD)
    five_hour = snapshot.window("five_hour")
    assert five_hour.used_percent == 23.5
    assert five_hour.remaining_percent == 76.5
    assert snapshot.window("seven_day").remaining_percent == pytest.approx(58.8)


def test_reset_timestamps_are_preserved():
    snapshot = parse_claude_statusline(DOCUMENTED_PAYLOAD)
    assert snapshot.window("five_hour").resets_at == 1738425600
    assert snapshot.window("seven_day").resets_at == 1738857600


def test_context_window_used_percentage_is_not_mistaken_for_quota():
    """`context_window.used_percentage` (8) sits next to the quota fields."""
    snapshot = parse_claude_statusline(DOCUMENTED_PAYLOAD)
    assert {w.used_percent for w in snapshot.windows} == {23.5, 41.2}


def test_budget_window_is_the_weekly_one_when_both_are_present():
    assert parse_claude_statusline(DOCUMENTED_PAYLOAD).longest_window().id == "seven_day"


# --- documented absences ------------------------------------------------------

def test_missing_rate_limits_yields_no_windows_and_no_false_unreadable():
    """Early in a session `rate_limits` is simply not there yet.

    That is a "not available yet", not a "host is reporting garbage", so it must
    not be recorded as an unreadable window.
    """
    payload = {k: v for k, v in DOCUMENTED_PAYLOAD.items() if k != "rate_limits"}
    snapshot = parse_claude_statusline(payload)
    assert snapshot.windows == []
    assert snapshot.unreadable_windows == []


def test_absent_rate_limits_does_not_overwrite_a_good_snapshot(tmp_path):
    """Otherwise one early statusLine call would erase usable quota state."""
    store = StateStore(tmp_path)
    store.save_snapshot(parse_claude_statusline(DOCUMENTED_PAYLOAD))

    payload = {k: v for k, v in DOCUMENTED_PAYLOAD.items() if k != "rate_limits"}
    empty = parse_claude_statusline(payload)
    assert not (empty.windows or empty.unreadable_windows), (
        "the CLI skips persisting in exactly this case"
    )
    assert store.load_snapshot().window("seven_day") is not None


@pytest.mark.parametrize("dropped, kept", [
    ("seven_day", "five_hour"),
    ("five_hour", "seven_day"),
])
def test_each_window_may_be_absent_independently(dropped, kept):
    payload = json.loads(json.dumps(DOCUMENTED_PAYLOAD))
    del payload["rate_limits"][dropped]
    snapshot = parse_claude_statusline(payload)
    assert [w.id for w in snapshot.windows] == [kept]
    assert snapshot.longest_window().id == kept


def test_no_quota_at_all_is_conserve_not_normal(tmp_path):
    """An API-key user, or a session before its first response, is not "full"."""
    session = TaskSession(
        task="t", provider="claude", window="seven_day",
        floor_percent=70, started_remaining_percent=95, cwd=str(tmp_path),
    )
    decision = unknown_quota_decision(session)
    assert decision.state == LandingState.CONSERVE
    assert decision.quota_known is False
