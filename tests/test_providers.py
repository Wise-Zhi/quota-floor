from agent_landing.providers import parse_claude_statusline


def test_parse_claude_rate_limits():
    snapshot = parse_claude_statusline({
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": 123},
            "seven_day": {"used_percentage": 30, "resets_at": 456},
        }
    })
    assert snapshot.provider == "claude"
    assert snapshot.window("five_hour").remaining_percent == 87.5
    assert snapshot.window("seven_day").remaining_percent == 70
