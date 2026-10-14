from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from time import time
from typing import Any

from .budget import BudgetRefused, start_budget
from .handoff import landing_prompt, save_reply_handoff, write_fallback_handoff
from .invocation import extract_invocation, parse_invocation, undecorate
from .models import LandingState, PolicyDecision, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import estimate_landing_reserve, evaluate, unknown_quota_decision
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
    # Read-only inspection, so a handoff can describe what it hands off. In
    # ChatGPT Work mode the agent's first LAND move, `Get-Location; rg --files
    # -g fib.py .`, was denied, and the handoff could only say "nothing read".
    # Every flag here only reads. Absent on purpose: `rg --pre`/`-z` (run
    # programs), `find -exec`/`-delete`/`-fprint`, and any cmdlet that writes.
    (("rg",), frozenset({
        "-n", "--line-number", "-N", "--no-line-number", "-i", "--ignore-case",
        "-s", "--case-sensitive", "-S", "--smart-case", "-l", "--files-with-matches",
        "--files", "-g", "--glob", "-t", "--type", "-T", "--type-not", "-c", "--count",
        "-C", "--context", "-A", "--after-context", "-B", "--before-context",
        "-w", "--word-regexp", "-F", "--fixed-strings", "-e", "--regexp", "--hidden",
        "--no-ignore", "-u", "-uu", "-m", "--max-count", "--max-depth", "--no-heading",
        "--heading", "-v", "--invert-match", "-x", "--line-regexp", "-o", "--only-matching",
        "-H", "--with-filename", "--sort",
    })),
    *(((name,), frozenset({
        "-path", "-literalpath", "-totalcount", "-head", "-tail", "-raw", "-encoding",
        "-first", "-last", "-n",
    })) for name in ("get-content", "gc", "cat", "type", "head", "tail")),
    *(((name,), frozenset({
        "-path", "-literalpath", "-filter", "-recurse", "-name", "-file", "-directory",
        "-force", "-depth", "-include", "-exclude", "-l", "-a", "-la", "-al", "-r", "-1",
        "-h", "-lh",
    })) for name in ("get-childitem", "gci", "dir", "ls")),
    *(((name,), frozenset()) for name in ("get-location", "gl", "pwd")),
    *(((name,), frozenset({
        "-pattern", "-path", "-literalpath", "-simplematch", "-casesensitive", "-context",
        "-list", "-notmatch", "-allmatches",
    })) for name in ("select-string", "sls")),
    (("find",), frozenset({
        "-name", "-iname", "-type", "-maxdepth", "-mindepth", "-path", "-ipath", "-print",
    })),
    (("wc",), frozenset({"-l", "-w", "-c"})),
)

#: Read-only commands may be chained with these, each part checked on its own.
#: Pipes and redirects stay out entirely: `cat a > b` and `gc a | Set-Content b`
#: are writes however innocent the first command is.
_COMMAND_SEPARATOR = re.compile(r"\s*(?:;|&&)\s*")


def is_safe_landing_command(command: str) -> bool:
    """Is this shell command read-only enough to run while landing?

    Operands (paths, `-k` expressions) pass freely because they cannot write on
    their own; only tokens that look like flags are checked, and an unrecognized
    flag is a refusal rather than a pass.
    """
    parts = _COMMAND_SEPARATOR.split(command.strip())
    return bool(parts) and all(_is_safe_single_command(part) for part in parts)


def _is_safe_single_command(command: str) -> bool:
    if not command or _SHELL_CONTROL.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quoting: refuse rather than guess at what it would run as.
        return False
    if not tokens:
        return False

    # PowerShell names and parameters are case-insensitive; `rg -C` and `rg -c`
    # are different flags, so an exact match is tried first.
    lowered = [token.lower() for token in tokens]
    for prefix, allowed_flags in _SAFE_LANDING_COMMANDS:
        if tuple(lowered[: len(prefix)]) != prefix:
            continue
        rest = tokens[len(prefix):]
        return all(
            _flag_name(token) in allowed_flags or _flag_name(token).lower() in allowed_flags
            for token in rest
            if token.startswith("-")
        )
    return False


def _flag_name(token: str) -> str:
    """`--tb=short` -> `--tb`, so a flag is judged by its name, not its value."""
    return token.split("=", 1)[0]


def is_safe_landing_tool(payload: dict[str, Any]) -> bool:
    """May this run in LAND? Reads and an allowlist of inspection commands only.

    No write is ever allowed, the handoff included: it is the final reply now,
    saved by the Stop hook, so the gate has no exception to get wrong.
    """
    name = _tool_name(payload)
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


def _concurrent_reserve(store: StateStore) -> float:
    """Landing reserve owed to *other* live budgets sharing this quota pool.

    A conversation's budget is its own, but the quota is the account's. Ignoring
    the other budgets is how two tasks both reach their floor and then discover
    there is room for one handoff between them.
    """
    total = 0.0
    for key, other in store.iter_sessions():
        if key == store.session_key or other.state == LandingState.STOPPED:
            continue
        total += estimate_landing_reserve(other.burn_samples, other.config)
    return total


def evaluate_current(
    store: StateStore,
    count_tool_call: bool = False,
    tool_use_id: str | None = None,
) -> tuple[TaskSession, PolicyDecision] | None:
    session = store.load_session()
    if not session or session.is_inactive:
        # No budget was requested, or its one task ended. Either way the guard
        # has no mandate over later work or another conversation.
        return None
    if count_tool_call and not _already_counted(session, tool_use_id):
        session.tool_calls_since_snapshot += 1
        if tool_use_id:
            session.last_counted_tool_use_id = tool_use_id

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
            concurrent_reserve_percent=_concurrent_reserve(store),
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


def _already_counted(session: TaskSession, tool_use_id: str | None) -> bool:
    """Has this exact tool call been counted by an earlier hook invocation?

    A host can invoke the same hook more than once for one tool call when the
    config is registered at two scopes. Without an identity to compare, the
    duplicate looks like extra work and silently deflates the per-call burn.
    Hosts that send no id keep the old behaviour, which over- rather than
    under-estimates the reserve.
    """
    return bool(tool_use_id) and session.last_counted_tool_use_id == tool_use_id


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


#: Landing subcommands that weaken or end the contract. An agent may read state
#: freely; it may not release itself. The one exception, a bare `complete`, is
#: answered by `is_complete_command` before this list is consulted.
_RELEASING_SUBCOMMANDS = ("cancel", "complete", "resume", "start", "invoke", "uninstall")


def _releases_the_guard(payload: dict[str, Any]) -> bool:
    """Is this tool call Landing being asked to stand itself down?"""
    if _tool_name(payload) not in {"Bash", "shell", "exec_command"}:
        return False
    tool_input = _tool_input(payload)
    command = str(tool_input.get("command") or tool_input.get("cmd") or "").casefold()
    if "agent_landing" not in command and "landing" not in command:
        return False
    return any(re.search(rf"\b{verb}\b", command) for verb in _RELEASING_SUBCOMMANDS)


def is_complete_command(payload: dict[str, Any]) -> bool:
    """Is this tool call exactly `landing complete`, with nothing riding along?

    Matched strictly, like the LAND allowlist: no shell control characters and no
    further arguments, so `landing complete && <anything>` stays a release and is
    denied. Only the trailing tokens are compared, because the interpreter path
    in front of `-m agent_landing.cli` differs per machine.
    """
    if _tool_name(payload) not in {"Bash", "shell", "exec_command"}:
        return False
    tool_input = _tool_input(payload)
    command = str(tool_input.get("command") or tool_input.get("cmd") or "").strip()
    if not command or _SHELL_CONTROL.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) == 4 and tokens[1:] == ["-m", "agent_landing.cli", "complete"]:
        return True
    if len(tokens) == 2 and tokens[1] == "complete":
        name = re.split(r"[\\/]", tokens[0])[-1].casefold()
        return name in {"landing", "landing.exe", "agent-landing", "agent-landing.exe"}
    return False


def complete_session(session: TaskSession, snapshot: QuotaSnapshot | None) -> float | None:
    """Mark the task finished. The rest of the turn is frozen; the next prompt releases it."""
    remaining = None
    if snapshot and snapshot.provider == session.provider:
        window = snapshot.window(session.window)
        if window:
            remaining = window.remaining_percent
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    session.completed_at_remaining_percent = remaining
    return remaining


#: The reply to an intercepted `landing complete`. It is delivered as a denial
#: because the guard has already done the work; the reason says so, or the model
#: would read the denial as a failure and retry.
COMPLETED_REASON = (
    "Quota guard: task recorded as complete, and this conversation's budget is closed. "
    "The guard handled the command itself, so it did not need to run; this is the expected "
    "response, not an error. Give your final reply now and stop: tool use stays blocked "
    "until the user's next message, which releases the budget."
)


#: A Landing subcommand that opens or ends a budget, wherever it sits in a
#: command line. Requires whitespace after the program name, so a path such as
#: `docs/landing-start.md` is not a call.
_LANDING_SUBCOMMAND = re.compile(
    r"(?:\blanding(?:\.exe)?|\bagent-landing(?:\.exe)?|\bagent_landing\.cli)[\"']?\s+"
    r"(invoke|start|complete)\b",
    re.IGNORECASE,
)


def _shell_command(payload: dict[str, Any]) -> str | None:
    if _tool_name(payload) not in {"Bash", "shell", "exec_command"}:
        return None
    tool_input = _tool_input(payload)
    return str(tool_input.get("command") or tool_input.get("cmd") or "")


def _bare_invoke_arguments(command: str) -> tuple[str, str | None] | None:
    """`(message, provider)` when the command is exactly one `landing invoke`.

    The command is never run -- the gate answers it -- so only its words
    matter, not how a shell would expand them. Anything chained to it makes it
    not bare.
    """
    text = command.strip()
    if text.startswith("&"):
        text = text[1:].strip()  # PowerShell's call operator
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(set(token) <= set(";&|<>()") for token in tokens):
        return None
    program = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if program.endswith(("landing", "landing.exe")):
        rest = tokens[1:]
    elif program.startswith("python") and tokens[1:3] == ["-m", "agent_landing.cli"]:
        rest = tokens[3:]
    else:
        return None
    if not rest or rest[0].lower() != "invoke":
        return None
    message, provider, args = "", None, rest[1:]
    index = 0
    while index < len(args):
        if args[index] == "--provider" and index + 1 < len(args):
            provider = args[index + 1]
            index += 2
            continue
        if not args[index].startswith("--") and not message:
            message = args[index]
        index += 1
    return message, provider


def _agent_landing_command(payload: dict[str, Any], store: StateStore) -> dict[str, Any] | None:
    """Answer an agent's own `landing invoke`/`start`/`complete`, or None.

    A terminal command has no conversation, so letting one run opened or ended
    the *default* budget, which no chat's gate consults. Observed in ChatGPT
    Work mode: the hook missed a wrapped prompt, the agent ran `landing invoke`
    itself, and the task then ran unenforced. So these are never run: a bare
    `invoke` opens this conversation's budget here, and every other shape is
    denied, whether or not a budget exists yet.
    """
    command = _shell_command(payload)
    match = _LANDING_SUBCOMMAND.search(command or "")
    if not match:
        return None
    subcommand = match.group(1).lower()
    session = store.load_session()
    active = session is not None and not session.is_inactive

    if subcommand == "complete":
        if not is_complete_command(payload):
            if active:
                return None  # a release attempt; the gate's release check answers it
            return hook_json(decision="deny", reason=(
                "Run `landing complete` on its own, with nothing chained to it."
            ))
        if not active:
            return hook_json(decision="deny", reason=(
                "Quota guard: there is no active quota budget in this conversation, so there "
                "is nothing to complete. Finish your reply and stop."
            ))
        return None  # the gate's own completion handling takes it from here

    if subcommand == "start":
        if active:
            return None  # a release attempt; the gate's release check answers it
        return hook_json(decision="deny", reason=(
            "Quota budgets are opened from the chat, not by an agent's tool call. If the user "
            "asked for one, run `landing invoke \"<their whole message>\"` on its own instead."
        ))

    bare = _bare_invoke_arguments(command or "")
    if bare is None:
        return hook_json(decision="deny", reason=(
            "Run `landing invoke \"<the user's message>\"` on its own, with nothing chained to "
            "it; the guard opens this conversation's budget itself."
        ))
    message, provider = bare
    found = extract_invocation(message)
    parsed = parse_invocation(found[0] if found else message)
    if not parsed.ok:
        return hook_json(decision="deny", reason=parsed.message())
    if parsed.action == "cancel":
        return hook_json(decision="deny", reason=(
            "Ending a quota budget is the user's to do, not the agent's. Ask them to send "
            "`/landing cancel` (Codex: `$landing cancel`) as a whole message."
        ))
    snapshot = store.load_snapshot()
    if snapshot is None:
        return hook_json(decision="deny", reason=(
            "Could not start a quota budget: no quota reading is available yet. Tell the user."
        ))
    try:
        result = start_budget(
            store,
            snapshot,
            provider=provider or snapshot.provider,
            task=parsed.task,
            window=parsed.window,
            preserve=parsed.percent if parsed.means_leave else None,
            max_spend=None if parsed.means_leave else parsed.percent,
            cwd=payload.get("cwd") or os.getcwd(),
            # A tool call is the agent's channel: tightening is fine, lowering
            # a floor the user set is not.
            authorized_by_user=False,
        )
    except BudgetRefused as refusal:
        return hook_json(decision="deny", reason=f"Could not start a quota budget: {refusal}")
    return hook_json(decision="deny", reason=(
        f"Quota guard: budget opened for this conversation -- {result['interpretation']} "
        "The guard handled the command itself, so it did not need to run; this is the expected "
        "response, not an error. Do not run any landing command again; carry on with the task."
    ))


def _pre_tool_locked(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    handled = _agent_landing_command(payload, store)
    if handled is not None:
        return handled

    # Deliberately does NOT renew a completed segment: a tool call cannot prove a
    # new user instruction, so renewing here would let the agent work straight
    # past `landing complete` and spend the budget it was told to return.
    current = evaluate_current(
        store,
        count_tool_call=True,
        tool_use_id=str(payload.get("tool_use_id") or payload.get("toolUseId") or "") or None,
    )
    if not current:
        return {}
    session, decision = current

    # Completion is the one release an agent may perform, because it lowers the
    # spend to zero rather than lifting the floor: it freezes the rest of the
    # turn. It is handled here, not by letting the command run, because the hook
    # knows which conversation's budget this is and a terminal command does not.
    # Past the boundary the task is out of budget, not finished, so the call
    # falls through and the landing instructions (and handoff) still apply.
    if decision.state in (LandingState.NORMAL, LandingState.CONSERVE) and is_complete_command(payload):
        complete_session(session, store.load_snapshot())
        store.save_session(session)
        return hook_json(decision="deny", reason=COMPLETED_REASON)

    # Checked in every state, not only past the boundary. The gate guards the
    # *floor*, but standing the guard down is not a spend -- an agent that can
    # run `landing cancel` in NORMAL removes the floor before the gate ever has
    # an opinion, which is the same hole the start ratchet closed. Ending a
    # budget is the user's, through a channel an agent cannot forge.
    if _releases_the_guard(payload):
        return hook_json(
            decision="deny",
            reason=(
                "Ending a quota budget is the user's to do, not the agent's. "
                "Ask them to send `/landing cancel` (Codex: `$landing cancel`) "
                "as a whole message."
            ),
        )

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
        if is_safe_landing_tool(payload):
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
                "Quota budget boundary reached. Editing is blocked; write the technical "
                "handoff as your final reply, then stop. " + CANCEL_HINT
            ),
            context=context,
        )

    if decision.state == LandingState.EMERGENCY:
        session.handoff_path = str(write_fallback_handoff(session, decision, store.handoffs_dir))
        session.state = LandingState.STOPPED
        session.last_notified_state = LandingState.EMERGENCY
        session.stop_reason = "emergency"
        store.save_session(session)
        return hook_json(
            decision="deny",
            reason=(
                "Emergency quota floor reached. A deterministic handoff was saved to "
                f"`{session.handoff_path}` without another model call. Stop this task."
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
    return undecorate(prompt).casefold() in _CANCEL_INVOCATIONS


def suggests_cancel(prompt: str) -> bool:
    """Does the prompt read like a request to end the budget? Hint only."""
    text = (prompt or "").casefold()
    return any(phrase in text for phrase in _CANCEL_HINTS)


#: A budget request needs both a quota word and a percentage before it counts as
#: one. Either alone is ordinary conversation -- "70% of the tests pass", "the
#: quota endpoint is slow" -- and this must not fire on those.
_BUDGET_WORDS = ("quota", "budget", "配额", "额度")
_PERCENTAGE = re.compile(r"\d{1,3}\s*(?:%|percent|percentage point)")


def suggests_budget(prompt: str) -> bool:
    """Does this read like a budget request that forgot the marker? Hint only.

    Codex no longer ships a skill, so a plain-language request reaches nothing at
    all: without this the work simply runs unguarded and silently. A hint cannot
    open a budget -- the percentage would be a guess, and a guessed floor is not
    the user's floor -- but it can stop the silence.
    """
    text = (prompt or "").casefold()
    if extract_invocation(prompt) is not None:
        return False
    return bool(_PERCENTAGE.search(text)) and any(word in text for word in _BUDGET_WORDS)


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


def landing_command(action: str) -> str:
    """The exact command that will run on *this* machine, for the model to copy.

    With no Codex skill left, nothing tells the model how to invoke Landing, and
    the bare name is not always on PATH -- observed live: the model tried
    `landing complete`, hit "not on PATH", and spent a round trip hunting for a
    launcher. Naming the interpreter that is already running the hook cannot be
    wrong, because it is the same environment Landing is installed in.
    """
    executable = sys.executable
    quoted = f'"{executable}"' if " " in executable else executable
    return f"{quoted} -m agent_landing.cli {action}"


def _blocked(reason: str) -> dict[str, Any]:
    """Refuse the prompt outright. The model never runs, so it cannot narrate."""
    return {"decision": "block", "reason": reason, "systemMessage": reason}


def _refuse(store: StateStore, reason: str) -> dict[str, Any]:
    """Block the prompt, and leave the reason somewhere the user can still read.

    Codex renders a blocked prompt as the single word "Blocked" and surfaces no
    reason through any channel, so without this the refusal is a dead end.
    """
    store.save_refusal(reason)
    return _blocked(reason)


def _open_budget_from_prompt(
    prompt: str,
    store: StateStore,
    *,
    provider: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Open the budget here, before the model is given the turn.

    This is the point of the hook-first design. Leaving the budget to the model
    meant it had to load a skill, then spend a tool call on `landing invoke` --
    and Codex requires a progress message before a tool call, so every budgeted
    task opened with a paragraph about Landing instead of the work. No amount of
    skill wording removes that: the preamble is structural, caused by there
    being a tool call at all. There is no tool call now.

    It also closes a real hole. Model-mediated triggering meant a budget request
    could simply fail to trigger, leaving the user unguarded while believing
    otherwise. A hook parses every prompt deterministically.

    Returns a hook response when it handled the prompt, or None to fall through.
    """
    found = extract_invocation(prompt)
    if found is None:
        return None
    arguments, authoritative = found
    parsed = parse_invocation(arguments)
    if not parsed.ok:
        # Mid-message text that does not parse is prose about the command, not a
        # broken invocation -- blocking there would make Landing impossible to
        # discuss. See `extract_invocation`.
        if not authoritative:
            return None
        store.save_refusal(parsed.message())
        return _blocked(parsed.message())
    if parsed.action == "cancel":
        return None  # handled by the cancel path, which owns its own matching

    snapshot = store.load_snapshot()
    if snapshot is None or snapshot.provider != provider:
        return _refuse(store, (
            f"Could not start a quota budget: no {provider} quota reading is available yet, so "
            "there is nothing to budget against. Nothing was started, and the task was not run. "
            "Retry once quota has been read, or run `landing status` to see why it is missing."
        ))
    try:
        with store.lock():
            result = start_budget(
                store,
                snapshot,
                provider=provider,
                task=parsed.task,
                window=parsed.window,
                preserve=parsed.percent if parsed.means_leave else None,
                max_spend=None if parsed.means_leave else parsed.percent,
                cwd=payload.get("cwd") or os.getcwd(),
                # A UserPromptSubmit is the one channel an agent cannot forge, so
                # a floor arriving here is the user's own -- including a lower one.
                authorized_by_user=True,
            )
    except BudgetRefused as refusal:
        return _refuse(store, f"Could not start a quota budget: {refusal}")

    store.clear_refusal()

    # The model is told the budget exists and that there is nothing to do about
    # it, so it has no reason to mention the guard before starting the work.
    return _prompt_context(
        f"Quota guard: a budget is already active for this instruction -- {result['interpretation']} "
        "It was established before this turn began; do not run any landing command to set it up, "
        "and do not describe the guard, the budget, or this setup to the user -- in particular do not open by naming a skill or workflow you are applying. "
        "The budget ends by itself when this turn ends, so there is nothing to run when the "
        "task is done: just finish and stop, without inventing further work. "
        # Codex ships no skill, so this is its only copy of the ordering rule.
        "Do the most important part of the task that is useful on its own first, and leave each "
        "part finished before starting the next, so that if the guard lands midway the finished "
        "work is the part that matters most. This changes the order of the work, never its "
        "amount: do not estimate how much fits in the budget or cut scope up front. "
        + (
            f"The task is: {parsed.task}"
            if parsed.task
            else "No task was named, so the budget covers the work already under way "
                 "in this conversation. Carry on with it."
        )
    )


def user_prompt(
    payload: dict[str, Any], store: StateStore, provider: str = "claude"
) -> dict[str, Any]:
    """Act on an explicit user cancellation; never renew a completed task.

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

    opened = _open_budget_from_prompt(prompt, store, provider=provider, payload=payload)
    if opened is not None:
        return opened

    with store.lock():
        session = store.load_session()
        if (session is None or session.is_inactive) and not explicit and suggests_budget(prompt):
            return _prompt_context(
                "Quota guard: this reads like a request to work under a quota budget, but it "
                "carried no Landing invocation, so NO budget was opened and this work is "
                "unguarded. Say that plainly, and give the exact form: `/landing leave 70 "
                "<task>` on Claude Code, `$landing leave 70 [task]` on Codex. Do not open a "
                "budget yourself and do not invent a percentage the user did not give."
            )
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

        if session.is_complete:
            # `complete` freezes the rest of the current agent turn so the model
            # cannot declare victory and keep spending. A fresh UserPromptSubmit
            # proves control returned to the user; release the old task instead
            # of renewing it into an unrelated instruction or another chat.
            session.stop_reason = "completed_released"
            store.save_session(session)
            return {}

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

        return {}


def post_tool(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """Retired. The handoff no longer lands through a write; see `stop`.

    Kept as a no-op so a session that loaded the old hooks before an upgrade
    keeps running cleanly until it is restarted.
    """
    return {}


def stop(payload: dict[str, Any], store: StateStore) -> dict[str, Any]:
    """End the budget with the turn: release it, or in LAND save the handoff.

    A budget covers one instruction. Leaving it until the agent ran `complete`
    meant one that finished without doing so kept the floor armed in the chat,
    and every live budget claims a landing reserve from the shared quota -- two
    forgotten ones pushed a fresh `spend 5` straight into LAND. Nothing on the
    prompt side ends a budget except an explicit cancel: a host may deliver a
    message added mid-task as a prompt, and releasing there would leave the rest
    of the task unguarded. An interrupt fires no Stop, so the budget then ends
    with the next turn instead.

    In LAND, both hosts give a Stop hook the turn's final reply as
    `last_assistant_message`. The text is captured here, by Landing, so landing
    is still proved by something on disk rather than by the model saying so.
    """
    with store.lock():
        session = store.load_session()
        if not session or session.is_inactive:
            return {}
        if session.state in (LandingState.NORMAL, LandingState.CONSERVE) or session.is_complete:
            session.state = LandingState.STOPPED
            session.stop_reason = "completed_released"
            session.last_notified_state = None
            store.save_session(session)
            return {}
        if session.state != LandingState.LAND:
            return {}

        if session.last_notified_state != LandingState.LAND and not payload.get("stop_hook_active"):
            # LAND was reached by a reading that arrived after the last tool
            # call, so the model never saw the instruction. Its reply is an
            # ordinary answer, not a handoff: give it the turn to write one.
            current = evaluate_current(store)
            session = store.load_session()
            session.last_notified_state = LandingState.LAND
            store.save_session(session)
            reason = landing_prompt(session, current[1]) if current else (
                "Quota budget boundary reached. Write the technical handoff as your final reply, then stop."
            )
            return {"decision": "block", "reason": reason}

        reply = str(payload.get("last_assistant_message") or "").strip()
        if reply:
            path = save_reply_handoff(session, reply, store.handoffs_dir)
        else:
            current = evaluate_current(store)
            session = store.load_session()
            decision = current[1] if current else unknown_quota_decision(session)
            path = write_fallback_handoff(session, decision, store.handoffs_dir)

        session.state = LandingState.STOPPED
        session.last_notified_state = LandingState.STOPPED
        # Not renewable: the guard stopped this work because quota ran out.
        session.stop_reason = "landed"
        session.handoff_path = str(path)
        store.save_session(session)
        return {"systemMessage": f"Landing: quota floor reached; handoff saved to {path}"}
