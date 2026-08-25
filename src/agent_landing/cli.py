from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import time

from .dashboard import serve_dashboard
from .handoff import write_fallback_handoff
from .hooks import evaluate_current, post_tool, pre_tool, renew_completed_segment, user_prompt
from .invocation import parse_invocation
from .models import LandingConfig, LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import evaluate
from .runway import estimate_runway
from .providers import CodexAppServerProvider, parse_claude_statusline
from .state import StateStore


def _store(args: argparse.Namespace) -> StateStore:
    return StateStore(getattr(args, "cwd", None) or os.getcwd())


def _print_snapshot(snapshot: QuotaSnapshot) -> None:
    parts = [f"{w.id} {w.remaining_percent:.1f}% left" for w in snapshot.windows]
    # Name unreadable windows rather than letting them vanish: a window that is
    # silently absent is also a window that is silently unguarded.
    parts += [f"{entry['id']} quota unreadable" for entry in snapshot.unreadable_windows]
    print(" · ".join(parts) if parts else "quota unavailable")


def cmd_claude_statusline(args: argparse.Namespace) -> int:
    payload = json.load(sys.stdin)
    snapshot = parse_claude_statusline(payload)
    store = _store(args)
    # Persist a reading that is entirely unreadable too. Keeping the previous good
    # snapshot instead would let the guard keep judging against a number the host
    # is no longer reporting, with nothing telling the user it had gone blind.
    if snapshot.windows or snapshot.unreadable_windows:
        store.save_snapshot(snapshot)
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
        snapshot = CodexAppServerProvider(executable=getattr(args, "executable", "codex")).read()
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


#: Slash-command spelling of each window id, so a refusal can be restated in the
#: syntax the user actually typed rather than only in CLI flags.
_WINDOW_WORD = {"five_hour": "5h", "seven_day": "weekly"}


def _interpretation(
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


def cmd_start(args: argparse.Namespace) -> int:
    store = _store(args)
    snapshot = _current_snapshot(args, args.provider, store)
    # Naming a window scopes the floor to it; `auto` guards every window.
    floor_everywhere = args.window == "auto"
    if args.window == "auto":
        window = snapshot.longest_window()
        if not window:
            raise SystemExit("No quota windows are available; cannot start a budget.")
        args.window = window.id
    else:
        window = snapshot.window(args.window)
    if not window:
        available = ", ".join(w.id for w in snapshot.windows) or "none"
        raise SystemExit(
            f"Quota window {args.window!r} unavailable; available: {available}. "
            "Omit the window to let Agent Landing pick the longest one your plan exposes."
        )
    remaining = window.remaining_percent

    if args.preserve is not None:
        floor = args.preserve
        mode = "preserve"
        max_spend = None
    else:
        max_spend = args.max_spend
        floor = max(0.0, remaining - max_spend)
        mode = "max_spend"

    if floor > remaining:
        raise SystemExit(f"Requested floor {floor:.1f}% is above current remaining quota {remaining:.1f}%")

    # The floor is a limit on every window, so a window already at or below it
    # puts the budget in violation the moment it starts. Refusing here beats
    # opening a session that immediately demands an emergency landing, and it
    # surfaces the real situation while it is still free to act on.
    if floor_everywhere:
        mode_word = "spend" if mode == "max_spend" else "leave"
        shown = max_spend if mode == "max_spend" and max_spend is not None else floor
        breached = [w for w in snapshot.windows if w.remaining_percent <= floor]
        if breached:
            detail = ", ".join(f"{w.id} at {w.remaining_percent:.1f}%" for w in breached)
            raise SystemExit(
                f"Requested floor {floor:.1f}% has already been reached on {detail}. "
                "The floor applies to every quota window your plan exposes. Either lower "
                f"the floor, or scope it to one window: --window {args.window}, or "
                f"`{mode_word} {_WINDOW_WORD.get(args.window, args.window)} {shown:g}` "
                "in a /landing invocation."
            )

    config = LandingConfig(
        min_reserve_percent=args.min_reserve,
        default_reserve_percent=args.default_reserve,
        max_reserve_percent=args.max_reserve,
        landing_tool_calls=args.landing_tool_calls,
        safety_factor=args.safety_factor,
        conserve_margin_percent=args.conserve_margin,
        emergency_margin_percent=args.emergency_margin,
        max_snapshot_age_seconds=args.max_snapshot_age_seconds,
    )
    session = TaskSession(
        task=args.task,
        provider=args.provider,
        window=args.window,
        floor_percent=floor,
        started_remaining_percent=remaining,
        mode=mode,
        max_spend_percent=max_spend,
        handoff_path=args.handoff,
        cwd=str(Path(args.cwd or os.getcwd()).resolve()),
        last_remaining_percent=remaining,
        config=config,
        floor_applies_to_all_windows=floor_everywhere,
        started_remaining_by_window={
            w.id: w.remaining_percent for w in snapshot.windows
        },
    )
    store.save_session(session)
    store.save_snapshot(snapshot)
    decision = evaluate(session, remaining)
    scope_windows = [w.id for w in snapshot.windows] if floor_everywhere else [args.window]
    print(json.dumps({
        "provider": args.provider,
        "window": args.window,
        "starting_remaining_percent": round(remaining, 2),
        "final_floor_percent": round(floor, 2),
        "initial_landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_stops_near_percent": round(decision.development_floor_percent, 2),
        "handoff": args.handoff,
        "mode": mode,
        "floor_applies_to": scope_windows,
        # Always restated as a REMAINING floor, whichever way the user phrased it,
        # so "leave" and "spend" can never be silently swapped. Shown before any
        # work starts, so a misunderstanding surfaces while it is still free.
        "interpretation": _interpretation(
            mode, floor, max_spend, remaining, args.window, scope_windows
        ),
    }, indent=2))
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    """Single entry point for `/landing` (Claude) and `$landing` (Codex).

    Both hosts call this with the raw argument string, so the two syntaxes cannot
    drift apart and a user cannot learn one and be burned by the other.
    """
    parsed = parse_invocation(args.arguments)
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
    store = _store(args)
    if args.platform == "codex":
        _refresh_codex_if_needed(store, args.refresh_after_seconds, args.executable)
    payload = json.load(sys.stdin)
    print(json.dumps(pre_tool(payload, store), separators=(",", ":")))
    return 0


def cmd_hook_post(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = json.load(sys.stdin)
    print(json.dumps(post_tool(payload, store), separators=(",", ":")))
    return 0


def cmd_hook_user_prompt(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = json.load(sys.stdin)
    print(json.dumps(user_prompt(payload, store), separators=(",", ":")))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Manual equivalent of the UserPromptSubmit hook, for hosts that lack one."""
    store = _store(args)
    session = store.load_session()
    if not session:
        raise SystemExit("No quota-budgeted task to resume.")
    if not renew_completed_segment(session):
        raise SystemExit(
            f"Session is {session.state.value} (reason: {session.stop_reason or 'n/a'}) and is not "
            "renewable. Only a task that finished early can start a new segment; a landed or "
            "emergency stop means the quota itself ran out. Start a new budget explicitly."
        )
    store.save_session(session)
    print(json.dumps({
        "state": session.state.value,
        "segment": session.segment_count,
        "floor_percent": session.floor_percent,
        "message": "New task segment started under the existing floor.",
    }, indent=2))
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
            "Task completed. The quota floor is a limit, not a spending target; no further work is "
            "allowed on this task. The budget contract stays in force: a new instruction starts a new "
            f"segment under the same {session.floor_percent:.1f}% floor."
        ),
    }, indent=2))
    return 0

def cmd_cancel(args: argparse.Namespace) -> int:
    """End the budget contract entirely and stand the guard down."""
    store = _store(args)
    session = store.load_session()
    if not session:
        raise SystemExit("No quota-budgeted task to cancel.")
    if session.is_cancelled:
        print(json.dumps({"state": "cancelled", "message": "Budget was already cancelled."}, indent=2))
        return 0
    session.state = LandingState.STOPPED
    session.stop_reason = "cancelled"
    store.save_session(session)
    print(json.dumps({
        "state": "cancelled",
        "segments_completed": session.segment_count,
        "floor_percent": session.floor_percent,
        "message": (
            "Quota budget cancelled. The guard no longer restricts tool use, and a new "
            f"instruction will NOT restart the {session.floor_percent:.1f}% floor. "
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
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("claude-statusline", help="ingest Claude Code statusLine JSON and display quota")
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
    p.add_argument("--task", required=True)
    _add_budget_tuning_args(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser(
        "invoke",
        help="parse a raw /landing or $landing invocation and start the budget (refuses on typos)",
    )
    p.add_argument("arguments", help="the raw argument string, e.g. \"weekly 70 -- fix the tests\"")
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
        help="UserPromptSubmit gate: a new user instruction starts a new segment under the same floor",
    )
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.set_defaults(func=cmd_hook_user_prompt)

    p = sub.add_parser(
        "resume",
        help="start a new task segment under the existing floor (manual UserPromptSubmit equivalent)",
    )
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("status", help="show the current budget decision and observed burn-rate runway")
    p.add_argument("--compact", action="store_true", help="print a one-line terminal/statusline view")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "complete",
        help=(
            "end the current task because it finished before the floor; the budget stays "
            "in force and a new instruction starts a new segment"
        ),
    )
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser(
        "cancel",
        help="end the budget contract entirely and stop guarding (use `complete` to end just the task)",
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
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
