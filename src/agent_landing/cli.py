from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import time

from .dashboard import serve_dashboard
from .handoff import write_fallback_handoff
from .hooks import evaluate_current, post_tool, pre_tool
from .models import LandingConfig, LandingState, QuotaSnapshot, QuotaWindow, TaskSession
from .policy import evaluate
from .runway import estimate_runway
from .providers import CodexAppServerProvider, parse_claude_statusline
from .state import StateStore


def _store(args: argparse.Namespace) -> StateStore:
    return StateStore(getattr(args, "cwd", None) or os.getcwd())


def _print_snapshot(snapshot: QuotaSnapshot) -> None:
    parts = [f"{w.id} {w.remaining_percent:.1f}% left" for w in snapshot.windows]
    print(" · ".join(parts) if parts else "quota unavailable")


def cmd_claude_statusline(args: argparse.Namespace) -> int:
    payload = json.load(sys.stdin)
    snapshot = parse_claude_statusline(payload)
    store = _store(args)
    if snapshot.windows:
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
            "No current quota snapshot. For Claude, configure `landing claude-statusline` first; "
            "for testing you may pass --remaining."
        )
    return snapshot


def cmd_start(args: argparse.Namespace) -> int:
    store = _store(args)
    snapshot = _current_snapshot(args, args.provider, store)
    window = snapshot.window(args.window)
    if not window:
        available = ", ".join(w.id for w in snapshot.windows) or "none"
        raise SystemExit(f"Quota window {args.window!r} unavailable; available: {available}")
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

    config = LandingConfig(
        min_reserve_percent=args.min_reserve,
        default_reserve_percent=args.default_reserve,
        max_reserve_percent=args.max_reserve,
        landing_turns=args.landing_turns,
        safety_factor=args.safety_factor,
        conserve_margin_percent=args.conserve_margin,
        emergency_margin_percent=args.emergency_margin,
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
    )
    store.save_session(session)
    store.save_snapshot(snapshot)
    decision = evaluate(session, remaining)
    print(json.dumps({
        "provider": args.provider,
        "window": args.window,
        "starting_remaining_percent": round(remaining, 2),
        "final_floor_percent": round(floor, 2),
        "initial_landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_stops_near_percent": round(decision.development_floor_percent, 2),
        "handoff": args.handoff,
    }, indent=2))
    return 0


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
        _refresh_codex_if_needed(store, args.max_snapshot_age, args.executable)
    payload = json.load(sys.stdin)
    print(json.dumps(pre_tool(payload, store), separators=(",", ":")))
    return 0


def cmd_hook_post(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = json.load(sys.stdin)
    print(json.dumps(post_tool(payload, store), separators=(",", ":")))
    return 0


def _compact_status_line(session: TaskSession, decision, runway) -> str:
    headroom = max(0.0, decision.remaining_percent - decision.development_floor_percent)
    window = "5H" if session.window == "five_hour" else "W" if session.window == "seven_day" else session.window
    runway_text = "learning"
    if runway.available and runway.recent_runway_hours is not None:
        hours = runway.recent_runway_hours
        runway_text = f"~{hours:.1f}h" if hours >= 1 else f"~{round(hours * 60)}m"
    return (
        f"AL | {window} {decision.remaining_percent:.1f}->{decision.floor_percent:.1f}% "
        f"| safe +{headroom:.1f}% | {decision.state.value.upper()} | runway {runway_text}"
    )


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    current = evaluate_current(store)
    if not current:
        print("No active task with a usable quota snapshot.")
        return 1
    session, decision = current
    runway = estimate_runway(session, decision, store.load_history())
    if getattr(args, "compact", False):
        print(_compact_status_line(session, decision, runway))
        return 0
    payload = {
        "task": session.task,
        "provider": session.provider,
        "window": session.window,
        "state": decision.state.value,
        "remaining_percent": round(decision.remaining_percent, 2),
        "final_floor_percent": round(decision.floor_percent, 2),
        "landing_reserve_percent": round(decision.reserve_percent, 2),
        "development_floor_percent": round(decision.development_floor_percent, 2),
        "safe_development_headroom_percent": round(
            max(0.0, decision.remaining_percent - decision.development_floor_percent), 2
        ),
        "recent_burn_samples": session.burn_samples[-8:],
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
        "message": "Task completed. The quota floor is a limit, not a spending target; no further work is allowed.",
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
    session = TaskSession(
        task="demo",
        provider="demo",
        window="seven_day",
        floor_percent=args.preserve,
        started_remaining_percent=args.start,
        config=config,
    )
    samples = [float(x) for x in args.burn.split(",") if x.strip()]
    session.burn_samples = samples
    remaining = args.start
    print("remaining\tstate\treserve\tdev_floor")
    while remaining >= max(0, args.preserve - 1):
        decision = evaluate(session, remaining)
        print(f"{remaining:6.1f}%\t{decision.state.value:9s}\t{decision.reserve_percent:5.1f}%\t{decision.development_floor_percent:5.1f}%")
        remaining -= args.step
    return 0


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
    p.add_argument("--window", choices=["five_hour", "seven_day"], default="seven_day")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--preserve", type=float, help="minimum quota percentage to leave after landing")
    group.add_argument("--max-spend", type=float, help="maximum percentage points this task may spend")
    p.add_argument("--task", required=True)
    p.add_argument("--handoff", default="AGENT_HANDOFF.md")
    p.add_argument("--remaining", type=float, help="manual quota value for testing/bootstrap")
    p.add_argument("--executable", default="codex")
    p.add_argument("--min-reserve", type=float, default=3.0)
    p.add_argument("--default-reserve", type=float, default=4.0)
    p.add_argument("--max-reserve", type=float, default=15.0)
    p.add_argument("--landing-turns", type=int, default=3)
    p.add_argument("--safety-factor", type=float, default=1.5)
    p.add_argument("--conserve-margin", type=float, default=5.0)
    p.add_argument("--emergency-margin", type=float, default=0.75)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("hook-pre-tool", help="PreToolUse gate for Claude Code or Codex")
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.add_argument("--max-snapshot-age", type=float, default=20.0)
    p.add_argument("--executable", default="codex")
    p.set_defaults(func=cmd_hook_pre)

    p = sub.add_parser("hook-post-tool", help="mark handoff completion after the handoff write tool succeeds")
    p.add_argument("--platform", choices=["claude", "codex"], required=True)
    p.set_defaults(func=cmd_hook_post)

    p = sub.add_parser("status", help="show the current budget decision and observed burn-rate runway")
    p.add_argument("--compact", action="store_true", help="print a one-line terminal/statusline view")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("complete", help="stop immediately because the task finished before the budget floor")
    p.set_defaults(func=cmd_complete)

    p = sub.add_parser("fallback-handoff", help="write a no-model-call emergency handoff")
    p.set_defaults(func=cmd_fallback)

    p = sub.add_parser("dashboard", help="serve the local quota-budget dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("simulate", help="simulate the landing state machine")
    p.add_argument("--start", type=float, default=100.0)
    p.add_argument("--preserve", type=float, default=70.0)
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
