"""Pins the parser to a real `account/rateLimits/read` payload.

Captured 2026-08-25 from codex-cli 0.149.0-alpha.4.3 (Codex Desktop, Windows
11 build 26200) on a **Plus** account, reproduced verbatim apart from the
redacted credit id. Re-captured on 0.150.0-alpha.8: same keys, same polarity,
same units -- only the numbers moved. It is the one external contract Quota Floor depends on
for Codex quota, so a change on either side should fail here loudly rather than
show up as a guard that quietly stops guarding.

What this payload established, none of which was verifiable from tests alone:
  - Plus exposes BOTH windows at once (primary 300 min, secondary 10080 min).
    The earlier assumption that Plus was five-hour-only was wrong, and it was
    the reason `auto` pinned the floor to the window that never binds.
  - The percentage key is `usedPercent` -- camelCase, and *used*, not remaining.
  - `resetsAt` is integer Unix seconds, not milliseconds and not ISO 8601.
  - The response stream interleaves unrelated notifications, so responses must
    be matched by id rather than by position.
"""

import json

from agent_landing.providers import parse_codex_rate_limits

LIVE_PAYLOAD = json.loads("""
{
  "rateLimits": {
    "limitId": "codex",
    "limitName": null,
    "primary": {"usedPercent": 6, "windowDurationMins": 300, "resetsAt": 1787687373},
    "secondary": {"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": 1788274173},
    "credits": {"hasCredits": false, "unlimited": false, "balance": "0"},
    "individualLimit": null,
    "spendControlReached": false,
    "planType": "plus",
    "rateLimitReachedType": null
  },
  "rateLimitsByLimitId": {
    "codex": {
      "limitId": "codex",
      "limitName": null,
      "primary": {"usedPercent": 6, "windowDurationMins": 300, "resetsAt": 1787687373},
      "secondary": {"usedPercent": 1, "windowDurationMins": 10080, "resetsAt": 1788274173},
      "credits": {"hasCredits": false, "unlimited": false, "balance": "0"},
      "individualLimit": null,
      "spendControlReached": false,
      "planType": "plus",
      "rateLimitReachedType": null
    }
  },
  "rateLimitResetCredits": {
    "availableCount": 1,
    "credits": [
      {
        "id": "RateLimitResetCredit_REDACTED",
        "resetType": "codexRateLimits",
        "status": "available",
        "grantedAt": 1787356269,
        "expiresAt": 1789948269,
        "title": "Full reset (Weekly + 5 hr)",
        "description": "Thanks for using Codex!"
      }
    ]
  }
}
""")


def test_a_plus_account_reports_both_windows():
    """The assumption that Plus is five-hour-only was false; both are present."""
    snapshot = parse_codex_rate_limits(LIVE_PAYLOAD)
    assert {w.id for w in snapshot.windows} == {"five_hour", "seven_day"}
    assert snapshot.unreadable_windows == []


def test_used_percent_is_read_as_consumed_not_remaining():
    snapshot = parse_codex_rate_limits(LIVE_PAYLOAD)
    five_hour = snapshot.window("five_hour")
    seven_day = snapshot.window("seven_day")
    assert (five_hour.used_percent, five_hour.remaining_percent) == (6.0, 94.0)
    assert (seven_day.used_percent, seven_day.remaining_percent) == (1.0, 99.0)


def test_window_ids_come_from_the_duration_not_the_position():
    """`primary`/`secondary` are ordering, not identity; the duration is truth."""
    swapped = json.loads(json.dumps(LIVE_PAYLOAD))
    limits = swapped["rateLimits"]
    limits["primary"], limits["secondary"] = limits["secondary"], limits["primary"]
    snapshot = parse_codex_rate_limits(swapped)
    assert snapshot.window("five_hour").used_percent == 6.0
    assert snapshot.window("seven_day").used_percent == 1.0


def test_resets_at_is_unix_seconds():
    snapshot = parse_codex_rate_limits(LIVE_PAYLOAD)
    assert snapshot.window("five_hour").resets_at == 1787687373
    assert snapshot.window("seven_day").resets_at == 1788274173


def test_an_unrecognised_percentage_key_is_reported_not_dropped():
    """If Codex renames the field, the window must go loud, not silent."""
    renamed = json.loads(json.dumps(LIVE_PAYLOAD))
    window = renamed["rateLimits"]["primary"]
    window["utilisation"] = window.pop("usedPercent")
    snapshot = parse_codex_rate_limits(renamed)
    assert [w.id for w in snapshot.windows] == ["seven_day"]
    assert [entry["id"] for entry in snapshot.unreadable_windows] == ["five_hour"]
