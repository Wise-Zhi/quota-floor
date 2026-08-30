"""A budget belongs to a conversation; the quota pool belongs to the account.

Those are two different things, and conflating them is what made a budget set
for one task block an unrelated task in another chat. State used to be keyed by
*location* -- first the repository, then the home directory -- and the move from
one to the other never re-asked what the key should be, even though both hosts
send `session_id` on every hook event.
"""

from pathlib import Path
from time import time

from agent_landing.hooks import evaluate_current, post_tool, pre_tool
from agent_landing.models import LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from agent_landing.state import StateStore

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "src/x.py"}, "tool_use_id": "t1"}


def snapshot(remaining: float = 90.0) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="claude",
        windows=[QuotaWindow(id="five_hour", used_percent=100 - remaining)],
        sampled_at=time(),
    )


def budget(store: StateStore, *, floor: float = 70.0, state: LandingState = LandingState.NORMAL,
           stop_reason: str | None = None, burn: list[float] | None = None) -> None:
    session = TaskSession(
        task="t", provider="claude", window="five_hour", floor_percent=floor,
        started_remaining_percent=90.0, cwd=str(store.root), state=state,
        stop_reason=stop_reason, burn_samples=burn or [],
    )
    store.save_session(session)


def test_a_budget_in_one_chat_does_not_reach_another(tmp_path):
    chat_a = StateStore(tmp_path, session_id="chat-a")
    chat_b = StateStore(tmp_path, session_id="chat-b")
    chat_a.save_snapshot(snapshot())
    budget(chat_a, floor=89.0)  # far above remaining: chat A must be gated

    assert pre_tool(WRITE, chat_a)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert pre_tool(WRITE, chat_b) == {}


def test_a_finished_budget_does_not_block_an_unrelated_chat(tmp_path):
    """The reported defect: a landed budget blocked a new, unrelated task.

    Quota had recovered by then, but a STOPPED session short-circuits before the
    quota is even looked at -- so it kept denying every tool call in every other
    conversation until the user cancelled it by hand.
    """
    finished = StateStore(tmp_path, session_id="yesterday")
    finished.save_snapshot(snapshot())
    budget(finished, state=LandingState.STOPPED, stop_reason="landed")

    fresh = StateStore(tmp_path, session_id="today")
    assert pre_tool(WRITE, fresh) == {}
    assert pre_tool(WRITE, finished)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_second_live_budget_raises_the_landing_reserve(tmp_path):
    """Reserves add up, because the pool does not.

    Two budgets that each hold their own reserve against one pool would both
    spend down to their floor and leave room for a single handoff between them.
    """
    alone = StateStore(tmp_path, session_id="alone")
    alone.save_snapshot(snapshot(remaining=80.0))
    budget(alone, floor=70.0)
    _, solo = evaluate_current(alone)

    other = StateStore(tmp_path, session_id="other")
    budget(other, floor=70.0, burn=[1.0, 1.0, 1.0])
    _, shared = evaluate_current(alone)

    assert shared.reserve_percent > solo.reserve_percent
    assert shared.development_floor_percent > solo.development_floor_percent


def test_a_stopped_neighbour_is_not_owed_a_reserve(tmp_path):
    store = StateStore(tmp_path, session_id="live")
    store.save_snapshot(snapshot(remaining=80.0))
    budget(store, floor=70.0)
    _, before = evaluate_current(store)

    done = StateStore(tmp_path, session_id="done")
    budget(done, floor=70.0, burn=[1.0, 1.0, 1.0], state=LandingState.STOPPED, stop_reason="landed")
    _, after = evaluate_current(store)

    assert after.reserve_percent == before.reserve_percent


def test_a_budget_written_before_this_layout_is_carried_forward(tmp_path):
    """Upgrading must not silently drop a live budget."""
    root = tmp_path / ".landing"
    root.mkdir(parents=True)
    legacy = TaskSession(
        task="from the old layout", provider="claude", window="five_hour",
        floor_percent=70.0, started_remaining_percent=90.0, cwd=str(tmp_path),
    )
    import json

    (root / "session.json").write_text(json.dumps(legacy.to_dict()), encoding="utf-8")

    store = StateStore(tmp_path, scope="project")
    carried = store.load_session()
    assert carried is not None and carried.task == "from the old layout"
    assert not (root / "session.json").exists()
