from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .handoff import landing_prompt, write_fallback_handoff
from .models import LandingState, PolicyDecision, TaskSession
from .policy import evaluate
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


def is_safe_landing_tool(payload: dict[str, Any], session: TaskSession) -> bool:
    name = _tool_name(payload)
    if is_handoff_write(payload, session):
        return True
    if name in READ_ONLY_TOOLS:
        return True
    if name in {"Bash", "shell", "exec_command"}:
        command = str(_tool_input(payload).get("command") or _tool_input(payload).get("cmd") or "")
        command = command.strip()
        safe = (
            r"git\s+(status|diff|log\s+-1|branch\s+--show-current|rev-parse)\b",
            r"(pytest|python\s+-m\s+pytest)\s+[^;&|]*$",
        )
        return any(re.match(pattern, command) for pattern in safe)
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


def evaluate_current(store: StateStore) -> tuple[TaskSession, PolicyDecision] | None:
    session = store.load_session()
    snapshot = store.load_snapshot()
    if not session or not snapshot or snapshot.provider != session.provider:
        return None
    window = snapshot.window(session.window)
    if not window:
        return None
    decision = evaluate(session, window.remaining_percent)
    session.state = decision.state
    store.save_session(session)
    return session, decision


def pre_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    current = evaluate_current(store)
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
                f"Quota guard: {decision.remaining_percent:.1f}% remains in {session.window}. "
                f"Landing begins near {decision.development_floor_percent:.1f}%. "
                "Avoid starting large new workstreams; finish the current bounded objective first."
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
        store.save_session(session)
        return hook_json(
            decision="deny",
            reason=(
                f"Emergency quota floor reached. A deterministic `{session.handoff_path}` was written without "
                "another model call. Stop this task."
            ),
        )

    return hook_json(decision="deny", reason="Quota-budgeted task is stopped. Start a new budget to continue.")


def post_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    session = store.load_session()
    if not session:
        return {}
    if session.state == LandingState.LAND and is_handoff_write(payload, session):
        session.state = LandingState.STOPPED
        session.last_notified_state = LandingState.STOPPED
        store.save_session(session)
    return {}
