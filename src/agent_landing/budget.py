"""Opening a budget, independent of who asked for it.

This used to live inside `cli.cmd_start`, reachable only by parsing argparse
namespaces. The `UserPromptSubmit` hook needs exactly the same logic -- so that a
budget opened by the host and a budget opened from the command line cannot mean
different things -- and hooks cannot import the CLI without a cycle. The logic
therefore lives here, and both callers are thin.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .models import LandingConfig, LandingState, QuotaSnapshot, TaskSession
from .policy import evaluate, window_floor, window_pressure
from .state import CorruptStateError, StateStore

#: Slash-command spelling of each window id, so a refusal can be restated in the
#: syntax the user actually typed rather than only in CLI flags.
WINDOW_WORD = {"five_hour": "5h", "seven_day": "weekly"}

DEFAULT_HANDOFF = "AGENT_HANDOFF.md"


class BudgetRefused(Exception):
    """A budget could not be opened; the message is meant for the user.

    Raised rather than returned because every refusal is terminal for its
    caller: the CLI turns it into an exit status, and the `UserPromptSubmit`
    hook turns it into a blocked prompt. Neither has anything else to do with it,
    and a refusal must never leave a half-created session behind.
    """


def interpretation(
    mode: str,
    floor: float,
    max_spend: float | None,
    remaining: float,
    window: str,
    scope_windows: list[str],
) -> str:
    """Restate the budget as a REMAINING floor, whichever way the user phrased it.

    Always names the windows the floor covers. A plan can expose more than one,
    and a floor that silently applied to only one of them was how `leave 70`
    ended up guarding the window that never binds.
    """
    covers_many = len(scope_windows) > 1
    listed = ", ".join(scope_windows)
    if mode == "max_spend" and max_spend is not None:
        lead = f"Spending at most {max_spend:.1f} percentage points"
        body = (
            f"on each of {listed}, measured from where that window stands right now; "
            f"work stops as soon as any one of them has given up {max_spend:.1f} points"
            if covers_many
            else f"of the {window} quota, stopping while at least {floor:.1f}% is still "
            f"REMAINING (currently {remaining:.1f}%)"
        )
    else:
        lead = f"Leaving at least {floor:.1f}%"
        body = (
            f"REMAINING on every quota window ({listed}); work stops as soon as any one of "
            f"them reaches {floor:.1f}%"
            if covers_many
            else f"REMAINING of the {window} quota (currently {remaining:.1f}%)"
        )
    return (
        f"{lead}: {body}. "
        "That floor is a limit, never a spending target -- if the task finishes sooner, "
        "the unused quota is returned to you."
    )


def start_budget(
    store: StateStore,
    snapshot: QuotaSnapshot,
    *,
    provider: str,
    task: str,
    window: str = "auto",
    preserve: float | None = None,
    max_spend: float | None = None,
    handoff: str = DEFAULT_HANDOFF,
    cwd: str | None = None,
    config: LandingConfig | None = None,
) -> dict[str, Any]:
    """Open a budget against `snapshot`, or raise `BudgetRefused`.

    The caller supplies the snapshot because acquiring one is provider-specific
    and sometimes interactive (`--remaining`, `--executable`); the decision of
    what a budget *means* is not, and lives here.
    """
    config = config or LandingConfig()
    # Naming a window scopes the floor to it; `auto` guards every window.
    floor_everywhere = window == "auto"
    if floor_everywhere:
        chosen = snapshot.longest_window()
        if not chosen:
            raise BudgetRefused("No quota windows are available; cannot start a budget.")
        window = chosen.id
    else:
        chosen = snapshot.window(window)
    if not chosen:
        available = ", ".join(w.id for w in snapshot.windows) or "none"
        raise BudgetRefused(
            f"Quota window {window!r} unavailable; available: {available}. "
            "Omit the window to let Agent Landing pick the longest one your plan exposes."
        )
    remaining = chosen.remaining_percent

    if preserve is not None:
        floor = preserve
        mode = "preserve"
        max_spend = None
    else:
        floor = max(0.0, remaining - (max_spend or 0.0))
        mode = "max_spend"

    if floor > remaining:
        raise BudgetRefused(
            f"Requested floor {floor:.1f}% is above current remaining quota {remaining:.1f}%"
        )

    try:
        previous = store.load_session()
    except CorruptStateError:
        # `landing start` is one of the documented ways out of a damaged session,
        # and the gate's own denial message names it. A floor that cannot be read
        # cannot be compared, so there is nothing to ratchet against; refusing
        # here would leave the user with a guard that denies everything and no
        # way to re-establish one. Corrupting the session first is not a route
        # around the ratchet either: the very next tool call is denied, so the
        # `start` never runs.
        previous = None
    if previous is not None and not (previous.is_inactive or previous.is_complete):
        # A budget already in force. Tightening it is always safe and needs no
        # permission; loosening it is the one thing an agent must never be able
        # to do, and `start` was a wide-open route to exactly that -- the gate
        # only guards the boundary, so in NORMAL an agent could replace a 70%
        # floor with 5% and keep going.
        #
        # POSIX rlimits solve the same problem the same way: a limit ratchets one
        # direction, and going the other way needs privilege. Here the privilege
        # is `/landing cancel`, which only a real user prompt can perform --
        # deliberately not a `--force` flag, because a flag is something the
        # agent can type, and that is the whole point.
        if floor < previous.floor_percent:
            raise BudgetRefused(
                f"A quota budget is already in force with a {previous.floor_percent:.1f}% floor "
                f"on {previous.window}{f' (task: {previous.task!r})' if previous.task else ''}. "
                f"Refusing to lower it to "
                f"{floor:.1f}%.\n"
                "Tightening a floor is always allowed; loosening one is not, because the guard "
                "would then be weaker than the user asked for.\n"
                "To replace it, end the contract first: send `/landing cancel` "
                "(Codex: `$landing cancel`) as a whole message, then start the new budget."
            )

    session = TaskSession(
        task=task,
        provider=provider,
        window=window,
        floor_percent=floor,
        started_remaining_percent=remaining,
        mode=mode,
        max_spend_percent=max_spend,
        handoff_path=handoff,
        cwd=str(Path(cwd or os.getcwd()).resolve()),
        last_remaining_percent=remaining,
        config=config,
        floor_applies_to_all_windows=floor_everywhere,
        started_remaining_by_window={w.id: w.remaining_percent for w in snapshot.windows},
    )

    if previous is not None and previous.provider == provider:
        # Burn measurement belongs to the account, not to one budget. Dropping it
        # reset the landing reserve from a measured value back to the 4% default
        # -- biasing the reserve downwards, the unsafe direction, and starting
        # the landing too late to finish a handoff. It also contradicted the
        # contract that keeps a cancelled session on disk precisely so this
        # history survives into the next budget.
        #
        # `segment_count` deliberately does NOT carry: a new budget is a new
        # contract, and segment 1 of it is the honest number.
        session.burn_samples = list(previous.burn_samples)
        session.previous_tasks = (
            previous.previous_tasks + ([previous.task] if previous.task else [])
        )[-10:]

    # The floor is a limit on every window, so a window already at or below it
    # puts the budget in violation the moment it starts. Refusing here beats
    # opening a session that immediately demands an emergency landing, and it
    # surfaces the real situation while it is still free to act on.
    #
    # Each window is asked the same question the gate would ask on the first tool
    # call, through the same two functions, so the pre-flight check and the guard
    # cannot disagree. Comparing every window against the one absolute number
    # derived from the budget window was wrong for `spend`, which is a per-window
    # delta: with the five-hour window normally further burned than the weekly
    # one, an ordinary `spend 15` was refused outright.
    if floor_everywhere:
        mode_word = "spend" if mode == "max_spend" else "leave"
        shown = max_spend if mode == "max_spend" and max_spend is not None else floor
        breached = []
        for w in snapshot.windows:
            limit = window_floor(session, w.id)
            state, _ = window_pressure(
                w.id, w.remaining_percent, limit, config.default_reserve_percent, config
            )
            if state == LandingState.EMERGENCY:
                breached.append((w, limit))
        if breached:
            detail = ", ".join(
                f"{w.id} at {w.remaining_percent:.1f}% (floor {limit:.1f}%)"
                for w, limit in breached
            )
            raise BudgetRefused(
                f"The requested floor has already been reached on {detail}. "
                "The floor applies to every quota window your plan exposes. Either lower "
                f"the floor, or scope it to one window: --window {window}, or "
                f"`{mode_word} {WINDOW_WORD.get(window, window)} {shown:g}` "
                "in a /landing invocation."
            )

    store.save_session(session)
    store.save_snapshot(snapshot)
    decision = evaluate(session, remaining)
    scope_windows = [w.id for w in snapshot.windows] if floor_everywhere else [window]
    # "If you change state, tell the user." Replacing a budget that was already
    # in force is exactly the result a user cannot model from their own input,
    # so it is named rather than left to be inferred from a changed number.
    replaced = None
    if previous is not None:
        replaced = {
            "previous_floor_percent": round(previous.floor_percent, 2),
            "previous_task": previous.task,
            "previous_state": previous.state.value,
            "was_cancelled": previous.is_cancelled,
            "burn_samples_carried_over": len(session.burn_samples),
        }
    return {
        "replaced_budget": replaced,
        "provider": provider,
        "window": window,
        "starting_remaining_percent": round(remaining, 2),
        "final_floor_percent": round(floor, 2),
        "initial_landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_stops_near_percent": round(decision.development_floor_percent, 2),
        "handoff": handoff,
        "mode": mode,
        "floor_applies_to": scope_windows,
        # Always restated as a REMAINING floor, whichever way the user phrased it,
        # so "leave" and "spend" can never be silently swapped. Shown before any
        # work starts, so a misunderstanding surfaces while it is still free.
        "interpretation": interpretation(
            mode, floor, max_spend, remaining, window, scope_windows
        ),
    }
