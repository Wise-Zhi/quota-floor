from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO
from time import time

from .dashboard import serve_dashboard
from .handoff import write_fallback_handoff
from .hooks import (
    cancel_session,
    evaluate_current,
    gate_failure,
    post_tool,
    pre_tool,
    user_prompt,
)
from .installation import HOSTS, InstallationConflict, doctor_host, install_host, uninstall_host
from .invocation import parse_invocation
from .budget import BudgetRefused, start_budget
from .models import LandingConfig, LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import evaluate, window_floor, window_pressure
from .runway import estimate_runway
from .providers import CodexAppServerProvider, parse_claude_statusline
from .state import SCOPES, CorruptStateError, StateStore, adopt_project_state


def _store(args: argparse.Namespace, payload: dict[str, Any] | None = None) -> StateStore:
    """The store for this call, bound to the conversation when one is known.

    Hosts send `session_id` on every hook event. A budget belongs to the
    conversation the user set it in, so the hooks pass it through; commands run
    from a terminal have no conversation and fall back to the default budget.
    """
    return StateStore(
        getattr(args, "cwd", None) or os.getcwd(),
        scope=getattr(args, "state_scope", None),
        session_id=(payload or {}).get("session_id"),
    )


def _reconfigure(stream: TextIO) -> None:
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        # A stream that cannot be reconfigured -- already detached, or replaced
        # by a test with something simpler -- is not a reason to lose the quota
        # reading this command exists to record.
        pass


def _print_snapshot(snapshot: QuotaSnapshot) -> None:
    parts = [f"{w.id} {w.remaining_percent:.1f}% left" for w in snapshot.windows]
    # Name unreadable windows rather than letting them vanish: a window that is
    # silently absent is also a window that is silently unguarded.
    parts += [f"{entry['id']} quota unreadable" for entry in snapshot.unreadable_windows]
    print(" · ".join(parts) if parts else "quota unavailable")


def _run_wrapped_status_line(command: str, raw_payload: str) -> str | None:
    """Run the user's own status line command and return its output.

    Claude Code has exactly one statusLine slot and exposes `rate_limits` there
    and nowhere else, so Landing has to share it rather than take it. The command
    receives the same payload Claude sent, because that is what it was written
    against.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            input=raw_payload,
            capture_output=True,
            text=True,
            # Claude Code speaks UTF-8 on this channel. `text=True` would
            # otherwise decode with the console code page -- cp949 here -- and
            # hand back mojibake, or raise, on any status line that is not
            # pure ASCII.
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"landing: wrapped status line failed: {error}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"landing: wrapped status line exited {result.returncode}: "
            f"{(result.stderr or '').strip()}",
            file=sys.stderr,
        )
        return None
    if result.stdout is None:
        # `text=True` decodes in a reader thread, and an exception there is
        # swallowed: the run returns 0 with stdout None. `errors="replace"`
        # above should make that unreachable, but reaching it must cost the
        # user a line on stderr, not their whole status bar.
        print("landing: wrapped status line produced no readable output", file=sys.stderr)
        return None
    return result.stdout.rstrip("\n")


def cmd_claude_statusline(args: argparse.Namespace) -> int:
    # This stdout is read by Claude Code, not by a console, and it reads UTF-8.
    # Python defaults to the locale encoding for a pipe, so on a non-UTF-8 code
    # page the separator arrived as mojibake in the status bar (seen on cp949),
    # and a wrapped status line with a character that code page lacks would
    # raise instead of printing.
    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)
    raw_payload = sys.stdin.read()
    payload = json.loads(raw_payload)
    snapshot = parse_claude_statusline(payload)
    store = _store(args)
    # Persist a reading that is entirely unreadable too. Keeping the previous good
    # snapshot instead would let the guard keep judging against a number the host
    # is no longer reporting, with nothing telling the user it had gone blind.
    if snapshot.windows or snapshot.unreadable_windows:
        store.save_snapshot(snapshot)
    if args.wrap:
        wrapped = _run_wrapped_status_line(args.wrap, raw_payload)
        if wrapped is not None:
            print(wrapped)
            return 0
        # Fall through to Landing's own line: an empty status bar would read as
        # Landing having broken the user's, and hide that quota is still being read.
    _print_snapshot(snapshot)
    return 0


def cmd_codex_snapshot(args: argparse.Namespace) -> int:
    snapshot = CodexAppServerProvider(executable=args.executable).read()
    _store(args).save_snapshot(snapshot)
    if args.json:
        print(json.dumps(snapshot.to_dict(), indent=2))
    else:
        _print_snapshot(snapshot)
    return 0


def _requested_hosts(value: str) -> tuple[str, ...]:
    return HOSTS if value == "all" else (value,)


def _run_install_command(args: argparse.Namespace, action: str) -> int:
    results = []
    failed = False
    for host in _requested_hosts(args.host):
        try:
            if action == "install":
                result = install_host(
                    host,
                    home=args.home,
                    python_executable=sys.executable,
                )
            elif action == "doctor":
                result = doctor_host(
                    host,
                    home=args.home,
                    live=not args.no_live,
                    codex_executable=args.codex_executable or "codex",
                )
            else:
                result = uninstall_host(host, home=args.home)
        except (InstallationConflict, OSError) as error:
            result = {"host": host, "status": "error", "error": str(error)}
        failed = failed or result["status"] == "error"
        results.append(result)
    payload = {"command": action, "results": results}
    if action == "install" and not failed:
        # Never announce readiness on its own. Writing the files is not the same
        # as arming them: Codex binds hook trust to the file's hash, so an
        # install -- including an *update* -- leaves every hook untrusted, and
        # untrusted hooks never run. Reporting "ready" there is the installer
        # producing the silently-unguarded state the guard exists to prevent.
        pending = [
            f"  {result['host']}: {result['next_action']}"
            for result in results
            if result.get("next_action")
        ]
        payload["message"] = (
            ("Installed, but NOT ENFORCING YET:\n" + "\n".join(pending) + "\n\n"
             if pending else "Landing is ready.\n")
            + "Use it where you already work:\n"
            "  Claude Code:  /landing leave 70 [task]\n"
            "  Codex:        $landing leave 70 [task]\n"
            "  Quick check:  landing status --compact\n"
            "  Full status:  landing status"
        )
    print(json.dumps(payload, indent=2))
    return 1 if failed else 0


def cmd_install(args: argparse.Namespace) -> int:
    # A budget written by an older version sits in the checkout it was started
    # in. Carry it up to the user-level root rather than letting an upgrade look
    # like the guard quietly forgetting the floor.
    store = _store(args)
    adopted = adopt_project_state(getattr(args, "cwd", None) or os.getcwd(), store.root)
    if adopted:
        print(
            f"landing: adopted the budget left in {adopted} into {store.root}. "
            "The original was left in place and can be deleted.",
            file=sys.stderr,
        )
    return _run_install_command(args, "install")


def cmd_doctor(args: argparse.Namespace) -> int:
    return _run_install_command(args, "doctor")


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _run_install_command(args, "uninstall")


def _current_snapshot(args: argparse.Namespace, provider: str, store: StateStore) -> QuotaSnapshot:
    if args.remaining is not None:
        if getattr(args, "window", None) in (None, "auto"):
            # `auto` means "every window this plan exposes", which a single
            # hand-written number cannot describe. Without this the synthetic
            # window was literally named "auto" and the budget was pinned to a
            # window that exists on no plan.
            raise SystemExit(
                "--remaining describes one window, so name it: "
                "--window five_hour or --window seven_day."
            )
        return QuotaSnapshot(
            provider=provider,
            windows=[QuotaWindow(id=args.window, used_percent=100.0 - args.remaining, source="manual")],
            sampled_at=time(),
        )
    if provider == "codex":
        executable = getattr(args, "executable", "codex")
        try:
            snapshot = CodexAppServerProvider(executable=executable).read()
        except Exception as error:
            # A missing `codex` on PATH used to surface as a raw FileNotFoundError
            # traceback, and the budget was then never created -- which leaves the
            # session unguarded while looking like a crash rather than a refusal.
            stored = store.load_snapshot()
            if stored and stored.provider == provider:
                return stored
            raise SystemExit(
                f"Could not read Codex quota via {executable!r}: {error}\n"
                "Point at the binary explicitly with --executable /path/to/codex, "
                "or pass --remaining together with --window for a manual value."
            )
        store.save_snapshot(snapshot)
        return snapshot
    snapshot = store.load_snapshot()
    if not snapshot or snapshot.provider != provider:
        raise SystemExit(
            "No quota snapshot yet, so there is nothing to budget against.\n"
            "Claude Code only reports `rate_limits` to the statusLine after the first API "
            "response of a session, and only for Claude.ai subscriptions, so this is expected "
            "if the budget is the very first thing requested in a new session.\n"
            "Send any message first and retry, or verify the statusLine hook with:\n"
            "  landing claude-statusline < a-saved-statusline-payload.json\n"
            "For testing you may pass --remaining."
        )
    return snapshot


def cmd_start(args: argparse.Namespace) -> int:
    store = _store(args)
    snapshot = _current_snapshot(args, args.provider, store)
    try:
        payload = start_budget(
            store,
            snapshot,
            provider=args.provider,
            task=args.task,
            window=args.window,
            preserve=args.preserve,
            max_spend=args.max_spend,
            handoff=args.handoff,
            cwd=args.cwd,
            config=LandingConfig(
                min_reserve_percent=args.min_reserve,
                default_reserve_percent=args.default_reserve,
                max_reserve_percent=args.max_reserve,
                landing_tool_calls=args.landing_tool_calls,
                safety_factor=args.safety_factor,
                conserve_margin_percent=args.conserve_margin,
                emergency_margin_percent=args.emergency_margin,
                max_snapshot_age_seconds=args.max_snapshot_age_seconds,
            ),
        )
    except BudgetRefused as refusal:
        raise SystemExit(str(refusal)) from refusal
    print(json.dumps(payload, indent=2))
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    """Single entry point for `/landing` (Claude) and `$landing` (Codex).

    Both hosts call this with the raw argument string, so the two syntaxes cannot
    drift apart and a user cannot learn one and be burned by the other.
    """
    parsed = parse_invocation(args.arguments)
    if parsed.ok and parsed.action == "cancel":
        # `/landing cancel` normally never reaches here -- the UserPromptSubmit
        # hook stands the guard down before the agent runs anything. This is the
        # fallback for hosts without that hook, and it keeps the two paths
        # meaning exactly the same thing.
        return cmd_cancel(args)
    if not parsed.ok:
        # Refuse rather than guess: silently starting work without a budget, or
        # with a misread floor, is worse than not starting at all.
        print(parsed.message(), file=sys.stderr)
        print(json.dumps(parsed.to_dict(), indent=2))
        return 2
    args.window = parsed.window
    args.preserve = parsed.percent if parsed.means_leave else None
    args.max_spend = None if parsed.means_leave else parsed.percent
    args.task = parsed.task
    return cmd_start(args)


def _refresh_codex_if_needed(store: StateStore, max_age: float, executable: str) -> None:
    session = store.load_session()
    if not session or session.provider != "codex":
        return
    age = store.snapshot_age_seconds()
    if age is None or age >= max_age:
        try:
            store.save_snapshot(CodexAppServerProvider(executable=executable).read())
        except Exception:
            # Fail open on telemetry transport errors; stale/unknown quota must not be fabricated.
            pass


def cmd_hook_pre(args: argparse.Namespace) -> int:
    """The gate. Always exits 0 and answers in JSON, including when it fails.

    Raising here would exit non-zero, which Claude Code reads as a non-blocking
    error and then runs the tool anyway -- a guard that crashes would silently
    stop guarding. Every failure is turned into an explicit decision instead.
    """
    store = _store(args)
    try:
        payload = json.load(sys.stdin)
        store = _store(args, payload)
        if args.platform == "codex":
            _refresh_codex_if_needed(store, args.refresh_after_seconds, args.executable)
        result = pre_tool(payload, store)
    except Exception as error:  # noqa: BLE001 - the gate must never fail open
        print(f"landing: gate error: {error}", file=sys.stderr)
        result = gate_failure(store, error)
    print(json.dumps(result, separators=(",", ":")))
    return 0


def cmd_hook_post(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        payload = json.load(sys.stdin)
        store = _store(args, payload)
        result = post_tool(payload, store)
    except Exception as error:  # noqa: BLE001
        # Fail *safe* rather than closed: this hook only ever ends a session, so
        # doing nothing leaves the guard running instead of releasing it.
        print(f"landing: post-tool hook error: {error}", file=sys.stderr)
        result = {}
    print(json.dumps(result, separators=(",", ":")))
    return 0


def cmd_hook_user_prompt(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        payload = json.load(sys.stdin)
        store = _store(args, payload)
        if args.platform == "codex":
            # A budget opened here needs a reading to open against, and on a
            # fresh session nothing has refreshed one yet.
            _refresh_codex_if_needed(store, args.refresh_after_seconds, args.executable)
        result = user_prompt(payload, store, provider=args.platform)
        if result.get("decision") == "block":
            print(result.get("reason", ""), file=sys.stderr)
    except Exception as error:  # noqa: BLE001
        # Fail safe: this hook cancels, releases, or opens a budget. Doing
        # nothing leaves the previous state in force, which is never the
        # dangerous direction -- unlike the pre-tool gate, which must deny.
        print(f"landing: user-prompt hook error: {error}", file=sys.stderr)
        result = {}
    print(json.dumps(result, separators=(",", ":")))
    return 0


def _compact_status_line(session: TaskSession, decision, runway) -> str:
    headroom = max(0.0, decision.remaining_percent - decision.development_floor_percent)
    window = "5H" if session.window == "five_hour" else "W" if session.window == "seven_day" else session.window
    runway_text = "learning"
    if runway.available and runway.recent_runway_hours is not None:
        hours = runway.recent_runway_hours
        runway_text = f"~{hours:.1f}h" if hours >= 1 else f"~{round(hours * 60)}m"
    line = (
        f"AL | {window} {decision.remaining_percent:.1f}->{decision.floor_percent:.1f}% "
        f"| safe +{headroom:.1f}% | {decision.state.value.upper()} | runway {runway_text}"
    )
    if not decision.quota_known:
        return line + " | QUOTA UNKNOWN"
    if decision.limiting_window:
        # Say which window forced the state, or the numbers above look wrong.
        return line + f" | limited by {decision.limiting_window}"
    return line


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    current = evaluate_current(store)
    if not current:
        # A cancelled session is deliberately invisible to the gate, but the user
        # asking for status deserves to be told the guard is standing down rather
        # than that nothing exists.
        # A blocked invocation is invisible on Codex, which renders it as the
        # single word "Blocked". Report it before anything older: the user is
        # asking because of the prompt that was just turned away.
        refusal = store.load_refusal()
        if refusal:
            if getattr(args, "compact", False):
                print("AL | last invocation refused | no budget")
            else:
                print(refusal)
            return 1
        session = store.load_session()
        if session and session.is_cancelled:
            if getattr(args, "compact", False):
                print("AL | budget cancelled | not guarding")
            else:
                print(json.dumps({
                    "state": "cancelled",
                    "task": session.task,
                    "segments_completed": session.segment_count,
                    "previous_floor_percent": session.floor_percent,
                    "message": "Quota budget cancelled; tool use is unrestricted. Start a new budget to be guarded.",
                }, indent=2))
            return 0
        print("No active task with a usable quota snapshot.")
        return 1
    session, decision = current
    runway = estimate_runway(session, decision, store.load_history())
    if getattr(args, "compact", False):
        print(_compact_status_line(session, decision, runway))
        return 0
    snapshot = store.load_snapshot()
    other_windows = {
        w.id: round(w.remaining_percent, 2)
        for w in (snapshot.windows if snapshot else [])
        if w.id != session.window
    }
    payload = {
        "task": session.task,
        "provider": session.provider,
        "window": session.window,
        "quota_known": decision.quota_known,
        "other_windows_remaining_percent": other_windows,
        "unreadable_windows": snapshot.unreadable_windows if snapshot else [],
        "limiting_window": decision.limiting_window,
        "state": decision.state.value,
        "remaining_percent": round(decision.remaining_percent, 2),
        "final_floor_percent": round(decision.floor_percent, 2),
        "landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_floor_percent": round(decision.development_floor_percent, 2),
        "safe_development_headroom_percent": round(
            max(0.0, decision.remaining_percent - decision.development_floor_percent), 2
        ),
        "recent_burn_samples_per_tool_call": session.burn_samples[-8:],
        "segment": session.segment_count,
        "previous_tasks": session.previous_tasks,
        "runway": runway.to_dict(),
        "handoff": session.handoff_path,
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    store = _store(args)
    session = store.load_session()
    if not session:
        raise SystemExit("No active quota-budgeted task.")
    snapshot = store.load_snapshot()
    remaining = None
    if snapshot and snapshot.provider == session.provider:
        window = snapshot.window(session.window)
        if window:
            remaining = window.remaining_percent
    session.state = LandingState.STOPPED
    session.stop_reason = "task_complete"
    session.completed_at_remaining_percent = remaining
    store.save_session(session)
    print(json.dumps({
        "state": "stopped",
        "reason": "task_complete",
        "remaining_percent": remaining,
        "floor_percent": session.floor_percent,
        "segment": session.segment_count,
        "message": (
            "Task completed before the quota floor. This task's guard is now released; "
            "a later Landing command starts a fresh budget."
        ),
    }, indent=2))
    return 0

def cmd_cancel(args: argparse.Namespace) -> int:
    """End the budget contract entirely and stand the guard down."""
    store = _store(args)
    try:
        session = store.load_session()
    except CorruptStateError as error:
        # Cancel is the documented way out of a denied gate, so it has to work on
        # exactly the state that caused the denial. Quarantining the unreadable
        # file leaves no session, which is what standing the guard down means.
        quarantined = store.quarantine_session()
        print(json.dumps({
            "state": "cancelled",
            "quarantined_state_file": str(quarantined) if quarantined else None,
            "message": (
                f"The budget state file was unreadable ({error}); it was moved aside and the "
                "guard has stood down. Tool use is unrestricted. Start a new budget to be "
                "guarded again."
            ),
        }, indent=2))
        return 0
    # Budgets are per conversation, and a terminal has none: `landing cancel`
    # typed at a shell cannot name the chat whose guard is blocking. So it ends
    # every live budget. That is the right default for the documented way out of
    # a denied gate -- cancelling too much costs a floor the user chose to drop,
    # while cancelling too little leaves them stuck with no way to say which.
    cancelled: list[dict[str, object]] = []
    for key, candidate in store.iter_sessions():
        if candidate.is_cancelled:
            continue
        cancel_session(candidate)
        _store(args, {"session_id": key}).save_session(candidate)
        cancelled.append({
            "session": key,
            "task": candidate.task,
            "floor_percent": candidate.floor_percent,
            "segments_completed": candidate.segment_count,
        })
    if not cancelled:
        if session or store.iter_sessions():
            print(json.dumps({"state": "cancelled", "message": "Budget was already cancelled."}, indent=2))
            return 0
        raise SystemExit("No quota-budgeted task to cancel.")
    floors = ", ".join(f"{entry['floor_percent']:.1f}%" for entry in cancelled)
    print(json.dumps({
        "state": "cancelled",
        "cancelled": cancelled,
        "message": (
            f"Quota budget cancelled ({len(cancelled)} active: {floors}). The guard no longer "
            "restricts tool use, and a new instruction will NOT restart those floors. "
            "Start a new budget to be guarded again."
        ),
    }, indent=2))
    return 0


def cmd_fallback(args: argparse.Namespace) -> int:
    store = _store(args)
    current = evaluate_current(store)
    if not current:
        raise SystemExit("No active task with a usable quota snapshot.")
    session, decision = current
    path = write_fallback_handoff(session, decision)
    print(path)
    return 0



def cmd_dashboard(args: argparse.Namespace) -> int:
    serve_dashboard(_store(args), host=args.host, port=args.port)
    return 0

def cmd_simulate(args: argparse.Namespace) -> int:
    config = LandingConfig()
    if args.max_spend is not None:
        floor = max(0.0, args.start - args.max_spend)
    else:
        floor = 70.0 if args.preserve is None else args.preserve
    session = TaskSession(
        task="demo",
        provider="demo",
        window="seven_day",
        floor_percent=floor,
        started_remaining_percent=args.start,
        config=config,
    )
    samples = [float(x) for x in args.burn.split(",") if x.strip()]
    session.burn_samples = samples
    remaining = args.start
    print("remaining\tstate\treserve\tdev_floor")
    while remaining >= max(0, floor - 1):
        decision = evaluate(session, remaining)
        print(f"{remaining:6.1f}%\t{decision.state.value:9s}\t{decision.reserve_percent:5.1f}%\t{decision.development_floor_percent:5.1f}%")
        remaining -= args.step
    return 0


def _add_budget_tuning_args(p: argparse.ArgumentParser) -> None:
    """Shared by `start` and `invoke` so the two cannot diverge."""
    p.add_argument("--handoff", default="AGENT_HANDOFF.md")
    p.add_argument("--remaining", type=float, help="manual quota value for testing/bootstrap")
    p.add_argument("--executable", default="codex")
    p.add_argument("--min-reserve", type=float, default=3.0)
    p.add_argument("--default-reserve", type=float, default=4.0)
    p.add_argument("--max-reserve", type=float, default=15.0)
    p.add_argument(
        "--landing-tool-calls",
        type=int,
        default=8,
        help="tool calls the landing sequence is expected to need (reserve is priced per tool call)",
    )
    p.add_argument("--safety-factor", type=float, default=1.5)
    p.add_argument("--conserve-margin", type=float, default=5.0)
    p.add_argument("--emergency-margin", type=float, default=0.75)
    p.add_argument(
        "--max-snapshot-age-seconds",
        type=float,
        default=900.0,
        help="a quota snapshot older than this is not trusted as current headroom",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="landing", description="Quota-budgeted graceful landing for coding agents")
    parser.add_argument("--cwd", help="project directory (defaults to current directory)")
    parser.add_argument(
        "--state-scope",
        choices=list(SCOPES),
        default=None,
        help=(
            "where the budget lives: 'user' (default) keeps one budget for the account, "
            "'project' keeps one per checkout. Overrides LANDING_STATE_SCOPE; "
            "LANDING_HOME still wins over both."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="install or update Landing for every project owned by this user")
    p.add_argument("--host", choices=["all", *HOSTS], default="all")
    p.add_argument("--home", help="override the user home directory (primarily for testing)")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("doctor", help="verify the persistent skill, hooks, and Codex trust state")
    p.add_argument("--host", choices=["all", *HOSTS], default="all")
    p.add_argument("--home", help="override the user home directory (primarily for testing)")
    p.add_argument("--codex-executable", help="path to Codex when it is not on PATH")
    p.add_argument("--no-live", action="store_true", help="inspect files only; do not query Codex app-server")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("uninstall", help="remove only Landing-managed user-level files and settings")
    p.add_argument("--host", choices=["all", *HOSTS], default="all")
    p.add_argument("--home", help="override the user home directory (primarily for testing)")
    p.set_defaults(func=cmd_uninstall, no_live=True, codex_executable=None)

    p = sub.add_parser("claude-statusline", help="ingest Claude Code statusLine JSON and display quota")
    p.add_argument(
        "--wrap",
        default=None,
        help=(
            "your own status line command. Landing ingests quota from the payload, then runs "
            "this with the same payload and prints its output instead of Landing's own line. "
            "Claude Code has one statusLine slot and reports quota only there, so Landing "
            "shares it rather than taking it."
        ),
    )
    p.set_defaults(func=cmd_claude_statusline)

    p = sub.add_parser("codex-snapshot", help="read Codex quota via app-server without a model turn")
    p.add_argument("--executable", default="codex")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_codex_snapshot)

    p = sub.add_parser("start", help="start a quota-budgeted task")
    p.add_argument("--provider", choices=["claude", "codex"], required=True)
    p.add_argument(
        "--window",
        choices=["auto", "five_hour", "seven_day"],
        default="auto",
        help=(
            "scope the floor to one window; the default 'auto' applies it to every "
            "window your plan exposes and stops at whichever reaches it first"
        ),
    )
    group = p.add_mutually_exclusive_group(required=True)
    # `--leave`/`--spend` match the `/landing leave|spend` wording; the original
    # names stay as aliases so existing scripts keep working.
    group.add_argument(
        "--leave", "--preserve", dest="preserve", type=float,
        help="minimum quota percentage that must remain after landing",
    )
    group.add_argument(
        "--spend", "--max-spend", dest="max_spend", type=float,
        help="maximum percentage points this task may spend",
    )
    p.add_argument(
        "--task",
        default="",
        help="optional label for the handoff; the budget does not need one",
    )
    _add_budget_tuning_args(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser(
        "invoke",
        help="parse a raw /landing or $landing invocation and start the budget (refuses on typos)",
    )
    p.add_argument("arguments", help="the raw argument string, e.g. \"leave weekly 70 fix the tests\"")
    p.add_argument("--provider", choices=["claude", "codex"], required=True)
    _add_budget_tuning_args(p)
    p.set_defaults(func=cmd_invoke)

    p = sub.add_parser("hook-pre-tool", help="PreToolUse gate for Claude Code or Codex")
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.add_argument(
        "--refresh-after-seconds",
        type=float,
        default=20.0,
        help=(
            "re-read Codex quota when the snapshot is older than this. Distinct from "
            "`start --max-snapshot-age-seconds`, which decides when a snapshot stops "
            "being trusted at all."
        ),
    )
    p.add_argument("--executable", default="codex")
    p.set_defaults(func=cmd_hook_pre)

    p = sub.add_parser("hook-post-tool", help="mark handoff completion after the handoff write tool succeeds")
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.set_defaults(func=cmd_hook_post)

    p = sub.add_parser(
        "hook-user-prompt",
        help="UserPromptSubmit gate: opens a budget from the invocation, or cancels one",
    )
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.add_argument("--executable", default="codex", help="path to Codex when it is not on PATH")
    p.add_argument("--refresh-after-seconds", type=float, default=60.0)
    p.set_defaults(func=cmd_hook_user_prompt)

    p = sub.add_parser("status", help="show the current budget decision and observed burn-rate runway")
    p.add_argument("--compact", action="store_true", help="print a one-line terminal/statusline view")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "complete",
        help="end the current task and release its budget guard",
    )
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser(
        "cancel",
        help="end the active budget early and stop guarding",
    )
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("fallback-handoff", help="write a no-model-call emergency handoff")
    p.set_defaults(func=cmd_fallback)

    p = sub.add_parser("dashboard", help="serve the local quota-budget dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("simulate", help="simulate the landing state machine")
    p.add_argument("--start", type=float, default=100.0)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--leave", "--preserve", dest="preserve", type=float, default=None)
    group.add_argument("--spend", "--max-spend", dest="max_spend", type=float, default=None)
    p.add_argument("--burn", default="0.5,0.7,0.6")
    p.add_argument("--step", type=float, default=2.0)
    p.set_defaults(func=cmd_simulate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except CorruptStateError as error:
        # The hooks answer this themselves; every other command reports it as a
        # refusal with a way out rather than as a traceback.
        print(
            f"landing: {error}\n"
            "The budget's terms cannot be recovered. Run `landing cancel` to move the file "
            "aside and stand the guard down, or `landing start ...` to establish a new budget.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
