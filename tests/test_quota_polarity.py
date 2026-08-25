"""Used vs remaining must be decided by field name, never inferred from a value.

30% used and 30% remaining are the same number. If polarity ever flips silently,
the guard inverts: "almost out" becomes "plenty left". These tests pin the one
internal unit (used_percent) at the provider boundary.
"""

from agent_landing.providers import parse_claude_statusline, used_percent_from


def test_used_field_is_taken_as_used():
    assert used_percent_from({"used_percentage": 30}) == 30.0
    assert used_percent_from({"usedPercent": 30}) == 30.0


def test_remaining_field_is_converted_not_copied():
    assert used_percent_from({"remainingPercent": 30}) == 70.0
    assert used_percent_from({"remaining_percentage": 30}) == 70.0
    assert used_percent_from({"percent_left": 90}) == 10.0


def test_absent_quota_is_unknown_rather_than_full():
    """Unknown is not 100% available."""
    assert used_percent_from({}) is None
    assert used_percent_from({"resets_at": 123}) is None


def test_contradictory_polarities_are_rejected():
    # 30 used + 30 remaining cannot both be true; trusting either would be a guess.
    assert used_percent_from({"usedPercent": 30, "remainingPercent": 30}) is None


def test_consistent_complementary_pair_is_accepted():
    assert used_percent_from({"usedPercent": 30, "remainingPercent": 70}) == 30.0


def test_claude_statusline_drops_unreadable_windows_instead_of_assuming_full():
    snapshot = parse_claude_statusline({
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5},
            "seven_day": {"resets_at": 456},          # no polarity -> unknown
            "model_scoped": {"usedPercent": 30, "remainingPercent": 30},  # contradictory
        }
    })
    assert snapshot.window("five_hour").remaining_percent == 87.5
    assert snapshot.window("seven_day") is None
    assert snapshot.window("model_scoped") is None


def test_remaining_style_statusline_is_still_read_correctly():
    snapshot = parse_claude_statusline({
        "rate_limits": {"seven_day": {"remainingPercent": 9}}
    })
    # 9% left must never be stored as 9% used (which would read as 91% left).
    assert snapshot.window("seven_day").remaining_percent == 9.0
    assert snapshot.window("seven_day").used_percent == 91.0


def test_unreadable_windows_are_named_not_silently_dropped():
    """A window that vanishes without explanation is also silently unguarded."""
    snapshot = parse_claude_statusline({
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5},
            "seven_day": {"resets_at": 456},
            "model_scoped": {"usedPercent": 30, "remainingPercent": 30},
        }
    })
    reported = {entry["id"]: entry["reason"] for entry in snapshot.unreadable_windows}
    assert set(reported) == {"seven_day", "model_scoped"}
    assert "no recognized used/remaining percentage field" in reported["seven_day"]
    assert "do not add up to 100" in reported["model_scoped"]


def test_readable_snapshot_reports_nothing_unreadable():
    snapshot = parse_claude_statusline({"rate_limits": {"seven_day": {"used_percentage": 9}}})
    assert snapshot.unreadable_windows == []


def test_unreadable_windows_survive_persistence(tmp_path):
    from agent_landing.models import QuotaSnapshot
    from agent_landing.state import StateStore

    original = parse_claude_statusline({"rate_limits": {"seven_day": {"resets_at": 1}}})
    store = StateStore(tmp_path)
    store.save_snapshot(original)
    assert store.load_snapshot().unreadable_windows == original.unreadable_windows


def test_old_snapshots_without_the_field_still_load():
    from agent_landing.models import QuotaSnapshot

    snapshot = QuotaSnapshot.from_dict({
        "provider": "claude",
        "windows": [{"id": "seven_day", "used_percent": 9.0}],
        "sampled_at": 1.0,
    })
    assert snapshot.unreadable_windows == []
