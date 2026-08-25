from __future__ import annotations

import json
import re
from pathlib import Path
from time import time
from typing import Any

from .handoff import landing_prompt, write_fallback_handoff
from .models import LandingState, PolicyDecision, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import evaluate, unknown_quota_decision
from .state import StateStore


READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "LS", "read_file", "list_dir", "search"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "write_file", "edit_file", "apply_patch"}


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool_name") or payload.get("toolName") or "")


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("tool_input") or payload.get("toolInput") or {}
    return raw if isinstance(raw, dict) else {}


def _candidate_path(tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "path", "filename"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def is_handoff_write(payload: dict[str, Any], session: TaskSession) -> bool:
    if _tool_name(payload) not in WRITE_TOOLS:
        return False
    path = _candidate_path(_tool_input(payload))
    if not path:
        return False
    return Path(path).name == Path(session.handoff_path).name


# An allowlisted prefix is worthless if the command can chain, redirect, or
# substitute its way past the gate: `git status && <any write>` would otherwise
# be permitted in LAND. Reject anything carrying shell control characters before
# the allowlist is consulted, then require a full match rather than a prefix.
_SHELL_CONTROL = re.compile(r"[;&|<>`$(){}\n\r]")

_SAFE_LANDING_COMMANDS = (
    r"git\s+(status|diff|log\s+-1|branch\s+--show-current|rev-parse)(\s+\S+)*",
    r"(pytest|python\s+-m\s+pytest)(\s+\S+)*",
)


def is_safe_landing_command(command: str) -> bool:
    command = command.strip()
    if not command or _SHELL_CONTROL.search(command):
        return False
    return any(re.fullmatch(pattern, command) for pattern in _SAFE_LANDING_COMMANDS)


def is_safe_landing_tool(payload: dict[str, Any], session: TaskSession) -> bool:
    name = _tool_name(payload)
    if is_handoff_write(payload, session):
        return True
    if name in READ_ONLY_TOOLS:
        return True
    if name in {"Bash", "shell", "exec_command"}:
        tool_input = _tool_input(payload)
        return is_safe_landing_command(str(tool_input.get("command") or tool_input.get("cmd") or ""))
    return False


def hook_json(decision: str | None = None, reason: str | None = None, context: str | None = None) -> dict[str, Any]:
    if not any((decision, reason, context)):
        return {}
    specific: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if decision:
        specific["permissionDecision"] = decision
    if reason:
        specific["permissionDecisionReason"] = reason
    if context:
        specific["additionalContext"] = context
    return {"hookSpecificOutput": specific}


def renew_completed_segment(session: TaskSession) -> bool:
    """Start a new task segment under the same floor after an early completion.

    The budget contract belongs to the user, not to one task: finishing a task
    must not silently drop the guard for whatever the user asks next. Only an
    early `landing complete` is renewable -- a landed or emergency stop means the
    quota itself ran out, and renewing there would defeat the floor.
    """
    if not session.is_renewable:
        return False
    if session.task:
        session.previous_tasks = (session.previous_tasks + [session.task])[-10:]
    session.segment_count += 1
    session.state = LandingState.NORMAL
    session.last_notified_state = None
    session.stop_reason = None
    session.completed_at_remaining_percent = None
    return True


def resolve_budget_window(session: TaskSession, snapshot: QuotaSnapshot) -> QuotaWindow | None:
    """Find the window this budget applies to, migrating if the plan changed.

    A session pinned to `seven_day` used to stop guarding entirely once the host
    stopped reporting that window -- e.g. moving to a plan that only exposes a
    five-hour limit. Silently not guarding is the worst possible failure, so the
    budget migrates to the longest window still available instead.
    """
    window = snapshot.window(session.window)
    if window:
        return window
    if any(entry.get("id") == session.window for entry in snapshot.unreadable_windows):
        # Present but unparseable is not the same as gone. Migrating here would
        # move the budget onto a window that is usually *more* permissive and
        # report the result as a known quota, so refuse to read anything instead.
        return None
    longest = snapshot.longest_window()
    if longest:
        session.window = longest.id
    return longest


def evaluate_current(
    store: StateStore,
    count_tool_call: bool = False,
) -> tuple[TaskSession, PolicyDecision] | None:
    session = store.load_session()
    if not session or session.is_cancelled:
        # No budget was requested, or the user ended the one they had. Either way
        # the guard has no mandate and must stay out of the way entirely.
        return None
    if count_tool_call:
        session.tool_calls_since_snapshot += 1

    snapshot = store.load_snapshot()
    window = None
    if snapshot and snapshot.provider == session.provider:
        window = resolve_budget_window(session, snapshot)
    if window is None or snapshot is None:
        decision = unknown_quota_decision(session)
    else:
        age = max(0.0, time() - snapshot.sampled_at)
        # The floor applies to every window, so `spend N` needs a baseline for
        # each one. Record it the first time a window is seen under this budget.
        for seen in snapshot.windows:
            session.started_remaining_by_window.setdefault(seen.id, seen.remaining_percent)
        others = [
            (w.id, w.remaining_percent) for w in snapshot.windows if w.id != window.id
        ]
        decision = evaluate(
            session,
            window.remaining_percent,
            snapshot_age_seconds=age,
            other_windows=others,
            unreadable_windows=[
                entry.get("id", "unknown") for entry in snapshot.unreadable_windows
            ],
        )
    session.state = decision.state
    store.save_session(session)
    return session, decision


def pre_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    # Deliberately does NOT renew a completed segment: a tool call cannot prove a
    # new user instruction, so renewing here would let the agent work straight
    # past `landing complete` and spend the budget it was told to return.
    current = evaluate_current(store, count_tool_call=True)
    if not current:
        return {}
    session, decision = current

    if decision.state == LandingState.NORMAL:
        return {}

    if decision.state == LandingState.CONSERVE:
        if session.last_notified_state != LandingState.CONSERVE:
            session.last_notified_state = LandingState.CONSERVE
            store.save_session(session)
            context = (
                f"Quota guard: {decision.describe_remaining()} in {session.window}. "
                f"Landing begins near {decision.development_floor_percent:.1f}%. "
                f"{decision.reason}"
            )
            return hook_json(context=context)
        return {}

    if decision.state == LandingState.LAND:
        context = landing_prompt(session, decision)
        if is_safe_landing_tool(payload, session):
            if session.last_notified_state != LandingState.LAND:
                session.last_notified_state = LandingState.LAND
                store.save_session(session)
                return hook_json(context=context)
            return {}
        session.last_notified_state = LandingState.LAND
        store.save_session(session)
        return hook_json(
            decision="deny",
            reason="Quota budget boundary reached. New implementation work is blocked; write the technical handoff.",
            context=context,
        )

    if decision.state == LandingState.EMERGENCY:
        write_fallback_handoff(session, decision)
        session.state = LandingState.STOPPED
        session.last_notified_state = LandingState.EMERGENCY
        session.stop_reason = "emergency"
        store.save_session(session)
        return hook_json(
            decision="deny",
            reason=(
                f"Emergency quota floor reached. A deterministic `{session.handoff_path}` was written without "
                "another model call. Stop this task."
            ),
        )

    return hook_json(decision="deny", reason="Quota-budgeted task is stopped. Start a new budget to continue.")


def user_prompt(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Renew the budget when the *user* sends a new instruction.

    This is the only place a completed segment may be renewed. A new user prompt
    is the one signal the agent cannot manufacture for itself, which keeps two
    rules true at once: the budget contract survives task boundaries, and the
    agent still cannot invent work after `landing complete`.
    """
    session = store.load_session()
    if not session or not renew_completed_segment(session):
        return {}
    store.save_session(session)
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"Quota guard: the previous task completed and a new segment (#{session.segment_count}) "
                f"has started under the same budget. The {session.window} floor is still "
                f"{session.floor_percent:.1f}% and is a hard limit, never a spending target."
            ),
        }
    }


def post_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    session = store.load_session()
    if not session:
        return {}
    if session.state == LandingState.LAND and is_handoff_write(payload, session):
        session.state = LandingState.STOPPED
        session.last_notified_state = LandingState.STOPPED
        # Not renewable: the guard stopped this work because quota ran out.
        session.stop_reason = "landed"
        store.save_session(session)
    return {}
