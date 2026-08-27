from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from time import time
from typing import Any

from .handoff import landing_prompt, write_fallback_handoff
from .models import LandingState, PolicyDecision, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import evaluate, unknown_quota_decision
from .state import CorruptStateError, StateStore


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


def _tool_failed(payload: dict[str, Any]) -> bool:
    """Did the host report this tool call as failed?"""
    response = payload.get("tool_response") or payload.get("toolResponse")
    if not isinstance(response, dict):
        return False
    return (
        response.get("success") is False
        or response.get("is_error") is True
        or bool(response.get("error"))
    )


def handoff_landed(payload: dict[str, Any], session: TaskSession) -> bool:
    """Confirm the handoff really exists before calling the task landed.

    `landed` is terminal and deliberately not renewable, so recording it for a
    write that failed would end the budget with no handoff at all -- precisely
    the outcome the landing reserve exists to prevent. The write tool reporting
    success is not enough on its own; the file has to be on disk.
    """
    if not is_handoff_write(payload, session) or _tool_failed(payload):
        return False
    path = Path(_candidate_path(_tool_input(payload)) or session.handoff_path)
    if not path.is_absolute():
        path = Path(session.cwd) / path
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


# An allowlisted prefix is worthless if the command can chain, redirect, or
# substitute its way past the gate: `git status && <any write>` would otherwise
# be permitted in LAND. Reject anything carrying shell control characters before
# the allowlist is consulted, then require a full match rather than a prefix.
_SHELL_CONTROL = re.compile(r"[;&|<>`$(){}\n\r]")

#: Flags accepted for pytest. Nothing here writes a file.
_PYTEST_FLAGS = frozenset({
    "-q", "--quiet", "-x", "--exitfirst", "-k", "-m", "-p", "--no-header",
    "--tb", "--maxfail", "--co", "--collect-only", "--lf", "--last-failed",
})

#: (command prefix, flags allowed after it). The prefix must match exactly and
#: every remaining token that looks like a flag must appear in the set.
#:
#: Allowlisting the *command* while accepting `(\s+\S+)*` for its arguments is a
#: blocklist wearing an allowlist's name, and it leaked twice: first through
#: chaining (`git status && <any write>`), then through writing flags
#: (`git diff --output=X`, `pytest --junitxml=X`) that turn a read-only command
#: into an arbitrary-path write. Unknown flags are refused by default here for
#: the same reason unknown quota is not treated as 100%.
_SAFE_LANDING_COMMANDS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("git", "status"), frozenset({"-s", "--short", "--porcelain", "-b", "--branch"})),
    (("git", "diff"), frozenset({
        "--stat", "--name-only", "--name-status", "--cached", "--staged", "--unified",
    })),
    (("git", "log", "-1"), frozenset({"--oneline", "--stat", "--format", "--pretty", "--no-patch"})),
    (("git", "branch", "--show-current"), frozenset()),
    (("git", "rev-parse"), frozenset({"--show-toplevel", "--abbrev-ref", "--git-dir", "--verify"})),
    (("pytest",), _PYTEST_FLAGS),
    (("python", "-m", "pytest"), _PYTEST_FLAGS),
)


def is_safe_landing_command(command: str) -> bool:
    """Is this shell command read-only enough to run while landing?

    Operands (paths, `-k` expressions) pass freely because they cannot write on
    their own; only tokens that look like flags are checked, and an unrecognized
    flag is a refusal rather than a pass.
    """
    command = command.strip()
    if not command or _SHELL_CONTROL.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quoting: refuse rather than guess at what it would run as.
        return False
    if not tokens:
        return False

    for prefix, allowed_flags in _SAFE_LANDING_COMMANDS:
        if tuple(tokens[: len(prefix)]) != prefix:
            continue
        rest = tokens[len(prefix):]
        return all(
            _flag_name(token) in allowed_flags
            for token in rest
            if token.startswith("-")
        )
    return False


def _flag_name(token: str) -> str:
    """`--tb=short` -> `--tb`, so a flag is judged by its name, not its value."""
    return token.split("=", 1)[0]


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


def renew_completed_segment(session: TaskSession, cwd: str | None = None) -> bool:
    """Start a new task segment under the same floor after an early completion.

    The budget contract belongs to the user, not to one task: finishing a task
    must not silently drop the guard for whatever the user asks next. Only an
    early `landing complete` is renewable -- a landed or emergency stop means the
    quota itself ran out, and renewing there would defeat the floor.

    The new segment adopts the directory the instruction came from. A budget is
    per account, so consecutive segments can run in different checkouts, and an
    emergency handoff has to land where the work actually is.
    """
    if not session.is_renewable:
        return False
    if cwd:
        session.cwd = str(Path(cwd).resolve())
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


#: Every denial names this, because a gate with no reachable exit is a trap. The
#: agent is denied `landing cancel` on purpose -- it must never lift its own floor
#: -- so the exit has to be something the *user* can do from wherever they are,
#: which is the chat, not necessarily a terminal.
CANCEL_HINT = (
    "Type `/landing cancel` (Claude Code) or `$landing cancel` (Codex) as the whole "
    "message to stand the guard down."
)


#: Shown when the gate cannot determine what it was asked to enforce. Names the
#: three ways out, because a deny with no exit is its own kind of trap.
UNREADABLE_STATE_REASON = (
    "Quota guard: the budget state file could not be read, so the terms of the budget "
    "are unknown. Work is blocked rather than continuing unguarded. Run `landing status` "
    "for detail, or `landing start ...` to re-establish the budget. "
    + CANCEL_HINT
)


def gate_failure(store: StateStore, error: BaseException) -> dict[str, Any]:
    """Fail closed -- but only where a budget was actually requested.

    A hook that raises exits non-zero, and Claude Code treats every exit code
    other than 2 as a non-blocking error: the tool then runs anyway. So a crash
    here silently un-guards the session, which is the failure this project
    ranks below having no guard at all. Deny instead.

    Where no session file exists there is no mandate to enforce, and blocking
    the user's work over an internal error would be pure harm, so that case
    stays out of the way exactly as it does on the healthy path.
    """
    if not store.session_path.exists():
        return {}
    reason = UNREADABLE_STATE_REASON
    if not isinstance(error, CorruptStateError):
        reason = (
            "Quota guard: the budget could not be evaluated "
            f"({type(error).__name__}: {error}). Work is blocked rather than continuing "
            "unguarded. Run `landing status` for detail. " + CANCEL_HINT
        )
    return hook_json(decision="deny", reason=reason)


def pre_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Evaluate the gate for one tool call, serialized against concurrent hooks.

    Unreadable state is answered here rather than left to the caller: the gate
    failing open is the outcome this whole module exists to prevent, so it must
    not depend on every call site remembering to catch.
    """
    with store.lock():
        try:
            return _pre_tool_locked(payload, store)
        except CorruptStateError as error:
            return gate_failure(store, error)


def _pre_tool_locked(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
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
            reason=(
                "Quota budget boundary reached. New implementation work is blocked; write the "
                "technical handoff. " + CANCEL_HINT
            ),
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

    return hook_json(
        decision="deny",
        reason="Quota-budgeted task is stopped. Start a new budget to continue. " + CANCEL_HINT,
    )


#: Spellings of an explicit, user-typed cancel. Matched against the WHOLE prompt
#: and never as a substring: "why does /landing cancel not work?" is a question
#: *about* the command, not an instruction to run it.
_CANCEL_INVOCATIONS = frozenset({"/landing cancel", "$landing cancel", "landing cancel"})

#: Phrases that merely *suggest* the budget should end. These only ever produce a
#: hint; they never cancel anything. Acting on them would mean guessing, and a
#: wrong guess silently removes the guard while the user still believes they are
#: covered -- the failure this project ranks below having no guard at all. The
#: list is best-effort and deliberately incomplete: a miss costs a hint, which is
#: exactly the behaviour that existed before.
_CANCEL_HINTS = (
    "cancel the budget", "cancel my budget", "cancel the quota budget",
    "stop watching my quota", "stop guarding", "forget the budget",
    "drop the budget", "end the budget", "lift the budget",
    "取消预算", "取消这个预算", "撤销预算", "关掉预算", "别管配额", "不用管配额",
)


def is_cancel_invocation(prompt: str) -> bool:
    """Did the user type the explicit cancel command, and nothing else?"""
    return " ".join((prompt or "").split()).casefold() in _CANCEL_INVOCATIONS


def suggests_cancel(prompt: str) -> bool:
    """Does the prompt read like a request to end the budget? Hint only."""
    text = (prompt or "").casefold()
    return any(phrase in text for phrase in _CANCEL_HINTS)


def cancel_session(session: TaskSession) -> bool:
    """End the contract and stand the guard down. False if already cancelled."""
    if session.is_cancelled:
        return False
    session.state = LandingState.STOPPED
    session.stop_reason = "cancelled"
    session.last_notified_state = None
    return True


def _prompt_context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def user_prompt(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Act on a new user instruction: cancel the budget, or renew a segment.

    This hook is the only channel that can prove the *user* asked for something --
    an agent cannot manufacture a `UserPromptSubmit`. That is what lets it change
    the contract at all, and it is why both privileged actions live here.

    Cancelling from this hook exists because the guard denies `landing cancel` as
    a tool call, which is correct (an agent must never lift its own floor) but
    used to leave the contract with no exit at all: a user away from a terminal
    could not stand down a guard that was blocking their agent. The hook already
    receives the user's own words, so it can simply do it.
    """
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    explicit = is_cancel_invocation(prompt)

    with store.lock():
        session = store.load_session()
        if not session:
            if explicit:
                return _prompt_context(
                    "Quota guard: there is no active quota budget to cancel. Tool use is "
                    "unrestricted. Tell the user, and do not run any landing command."
                )
            return {}

        if explicit:
            cancelled = cancel_session(session)
            if cancelled:
                store.save_session(session)
            return _prompt_context(
                "Quota guard: the user cancelled the quota budget, and it has already been "
                "stood down by this hook. Tool use is unrestricted and a later instruction "
                f"will NOT restart the {session.floor_percent:.1f}% floor. Tell the user it is "
                "cancelled and do not run any landing command for it."
                if cancelled
                else "Quota guard: the budget was already cancelled; nothing to do. Do not run "
                "any landing command for it."
            )

        if not session.is_cancelled and suggests_cancel(prompt):
            # Deliberately does not cancel: this is a guess, and a wrong guess
            # unguards the user silently. Point at the exact phrasing that works.
            return _prompt_context(
                "Quota guard: the user may be asking to end the quota budget, but that was not "
                "the explicit command, so nothing was changed. Tell the user to type "
                "`/landing cancel` (Claude Code) or `$landing cancel` (Codex) as the whole "
                "message. Do not attempt to cancel it yourself -- an agent cannot lift its own "
                "quota floor, and the attempt will be denied."
            )

        # `cwd` is where the host launched the hook, i.e. the project the user is
        # actually in right now -- which need not be the one the budget started in.
        if not renew_completed_segment(session, cwd=payload.get("cwd") or os.getcwd()):
            return {}
        store.save_session(session)

    return _prompt_context(
        f"Quota guard: the previous task completed and a new segment (#{session.segment_count}) "
        f"has started under the same budget. The {session.window} floor is still "
        f"{session.floor_percent:.1f}% and is a hard limit, never a spending target."
    )


def post_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    with store.lock():
        session = store.load_session()
        if not session:
            return {}
        if session.state == LandingState.LAND and handoff_landed(payload, session):
            session.state = LandingState.STOPPED
            session.last_notified_state = LandingState.STOPPED
            # Not renewable: the guard stopped this work because quota ran out.
            session.stop_reason = "landed"
            store.save_session(session)
    return {}
