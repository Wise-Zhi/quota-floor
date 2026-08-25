"""What happens when `codex` cannot be launched.

`landing start --provider codex` reads live quota by spawning `codex
app-server`. If that fails, the budget must either be built from the last stored
snapshot or refused out loud -- never crash, because a crash means no budget was
created and the session then runs unguarded while looking like a broken tool
rather than an unprotected one.
"""

from time import time

import pytest

from agent_landing.cli import build_parser
from agent_landing.models import QuotaSnapshot, QuotaWindow
from agent_landing.state import StateStore

MISSING = "definitely-not-a-real-codex-binary"


def start_argv(tmp_path):
    return ["--cwd", str(tmp_path), "start", "--provider", "codex",
            "--leave", "70", "--task", "t", "--executable", MISSING]


def test_a_missing_codex_binary_is_refused_readably(tmp_path):
    args = build_parser().parse_args(start_argv(tmp_path))
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)
    message = str(excinfo.value)
    assert MISSING in message
    assert "--executable" in message


def test_a_stored_snapshot_is_used_when_the_binary_is_gone(tmp_path, capsys):
    """A plausible reading already on disk beats refusing to start at all."""
    StateStore(tmp_path).save_snapshot(QuotaSnapshot(
        provider="codex",
        windows=[
            QuotaWindow(id="five_hour", used_percent=6.0),
            QuotaWindow(id="seven_day", used_percent=1.0),
        ],
        sampled_at=time(),
    ))
    args = build_parser().parse_args(start_argv(tmp_path))
    assert args.func(args) == 0
    assert StateStore(tmp_path).load_session().floor_percent == 70
