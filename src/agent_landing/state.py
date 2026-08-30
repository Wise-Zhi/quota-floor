from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from time import sleep, time
from typing import Iterator
from uuid import uuid4

from .models import QuotaSnapshot, TaskSession


class CorruptStateError(RuntimeError):
    """Local state is present but cannot be read.

    Deliberately distinct from "no session at all": a session file that exists
    but will not parse means a budget *was* requested and its terms are now
    unknown. Collapsing the two would let a damaged file read as "never
    guarded", which is the one failure mode this project treats as worse than
    having no guard.
    """


#: A lock older than this is assumed to belong to a hook process that died
#: mid-call. Hook processes live for well under a second.
_LOCK_STALE_SECONDS = 30.0


#: Windows raises PermissionError when the destination of a replace is open in
#: another process -- a concurrent reader, or a scanner. It clears in
#: milliseconds, so retry briefly rather than failing a tool call over it.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.02


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` in a single step so a crash cannot truncate it.

    The session file is rewritten on nearly every tool call, so a plain
    `write_text` makes a half-written file reachable rather than theoretical --
    and a half-written session file is one the gate can no longer read.

    The temporary name is unique per writer, not per process: hooks run in
    separate processes, but nothing stops two writers sharing one.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                sleep(_REPLACE_BACKOFF_SECONDS)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: State files carried between roots when a project-scoped budget is adopted.
STATE_FILES = ("session.json", "latest_snapshot.json", "snapshots.jsonl")

SCOPES = ("user", "project")
DEFAULT_SCOPE = "user"


def resolve_state_root(cwd: str | Path | None = None, scope: str | None = None) -> Path:
    """Where local state lives.

    Quota is account-wide, so the budget is too: the root is per user by
    default, not per repository. It used to be per repository, which meant a
    floor set in one checkout stopped guarding the moment the agent worked in
    another -- against the same quota -- and every new checkout started its burn
    history, and therefore its landing reserve, from scratch.

    `LANDING_HOME`/`AGENT_LANDING_HOME` still win outright; `project` scope is
    the explicit opt-out for anyone who wants a budget per checkout.
    """
    override = os.environ.get("LANDING_HOME") or os.environ.get("AGENT_LANDING_HOME")
    if override:
        return Path(override).expanduser().resolve()
    scope = scope or os.environ.get("LANDING_STATE_SCOPE") or DEFAULT_SCOPE
    if scope not in SCOPES:
        raise ValueError(f"Unknown state scope {scope!r}; expected one of {', '.join(SCOPES)}.")
    if scope == "project":
        return Path(cwd or os.getcwd()).resolve() / ".landing"
    return Path.home() / ".landing"


def adopt_project_state(cwd: str | Path, root: Path) -> Path | None:
    """Carry a budget left in a project-scoped root into the user-level one.

    Copied rather than moved: an upgrade that silently deletes a user's state is
    a worse trade than one that leaves a stale directory behind. Skipped
    entirely once the user-level root already holds a default budget, so it can
    never overwrite a live one.
    """
    source = Path(cwd).resolve() / ".landing"
    if source == root:
        return None
    source_session = source / "sessions" / f"{DEFAULT_SESSION_KEY}.json"
    if not source_session.exists():
        source_session = source / "session.json"  # written before budgets were per conversation
    if not source_session.exists():
        return None
    target_session = root / "sessions" / f"{DEFAULT_SESSION_KEY}.json"
    if target_session.exists() or (root / "session.json").exists():
        return None
    target_session.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target_session, source_session.read_text(encoding="utf-8"))
    for name in ("latest_snapshot.json", "snapshots.jsonl"):
        if (source / name).exists():
            _atomic_write(root / name, (source / name).read_text(encoding="utf-8"))
    return source


#: Characters kept from a host-supplied conversation id. Both hosts send UUIDs,
#: but the id reaches us as untrusted text and becomes a filename.
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")
DEFAULT_SESSION_KEY = "default"


def session_key(session_id: str | None) -> str:
    """Filename for one conversation's budget.

    The budget belongs to a *conversation*, not to a machine. Quota is
    account-wide -- which is why the snapshot is shared and lives at the root --
    but "the pool is shared" does not make the contract shared: it makes several
    contracts interact, which the policy handles by summing their reserves.
    Keying by location instead of conversation is what let a budget set for one
    task block an unrelated task in another chat.

    `default` is the fallback for callers with no conversation: the CLI, and any
    host that does not send `session_id`.
    """
    cleaned = _UNSAFE_KEY.sub("-", (session_id or "").strip())[:120]
    return cleaned or DEFAULT_SESSION_KEY


class StateStore:
    def __init__(
        self,
        cwd: str | Path | None = None,
        scope: str | None = None,
        session_id: str | None = None,
    ):
        self.root = resolve_state_root(cwd, scope)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_key = session_key(session_id)
        self.session_path = self.sessions_dir / f"{self.session_key}.json"
        # Shared, because quota really is account-wide.
        self.snapshot_path = self.root / "latest_snapshot.json"
        self.history_path = self.root / "snapshots.jsonl"
        # Per conversation, so parallel chats neither serialize on one lock nor
        # read each other's refusal.
        self.lock_path = self.sessions_dir / f"{self.session_key}.lock"
        self.refusal_path = self.sessions_dir / f"{self.session_key}.refusal.json"
        self._adopt_single_session_layout()

    def _adopt_single_session_layout(self) -> None:
        """Carry a budget written before budgets were per conversation."""
        legacy = self.root / "session.json"
        if self.session_key != DEFAULT_SESSION_KEY or self.session_path.exists():
            return
        if not legacy.exists():
            return
        try:
            os.replace(legacy, self.session_path)
        except OSError:
            pass

    def iter_sessions(self) -> "list[tuple[str, TaskSession]]":
        """Every conversation's budget, for commands and cross-budget policy."""
        found: list[tuple[str, TaskSession]] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            if path.name.endswith(".refusal.json"):
                continue
            try:
                found.append((path.stem, TaskSession.from_dict(json.loads(path.read_text(encoding="utf-8")))))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return found

    def _lock_is_stale(self) -> bool:
        try:
            return (time() - self.lock_path.stat().st_mtime) > _LOCK_STALE_SECONDS
        except OSError:
            return False

    @contextmanager
    def lock(self, timeout: float = 2.0) -> Iterator[bool]:
        """Serialize read-modify-write of the session across concurrent hooks.

        Hosts issue tool calls in parallel and each one runs the gate in its own
        process, so without this the calls interleave and lose each other's
        updates to the burn accounting -- which biases the landing reserve
        *downwards*, the unsafe direction. Atomic writes keep the file readable
        either way; the lock is what keeps the counts true.

        Degrades to proceeding unlocked after `timeout`: a lock that cannot be
        acquired is not worth stalling a user's tool call for, and the worst
        case without it is a lost update rather than a damaged file.
        """
        deadline = time() + timeout
        handle: int | None = None
        while True:
            try:
                handle = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                pass
            except OSError:
                # Windows reports a lock file caught mid-delete as a permission
                # error rather than FileExistsError. Both mean the same thing --
                # someone else holds it -- so both must retry. Treating this one
                # as "give up" silently dropped the lock under contention.
                pass
            if self._lock_is_stale():
                self._break_stale_lock()
                continue
            if time() >= deadline:
                break
            sleep(0.02)
        try:
            yield handle is not None
        finally:
            if handle is not None:
                os.close(handle)
                self._release_lock()

    def _break_stale_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            # Another waiter got there first, or the holder is still closing it.
            pass

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    #: A refusal is only worth keeping while the user might still be looking for
    #: it. Older than this and it is more likely to confuse than explain.
    REFUSAL_TTL_SECONDS = 3600.0

    def save_refusal(self, text: str) -> None:
        """Remember why an invocation was turned away.

        Codex shows a blocked prompt as the single word "Blocked" and surfaces
        no `reason`, `stopReason`, `systemMessage`, or hook stderr -- measured on
        0.150.0-alpha.8. Blocking is still right, because the alternative is
        letting the turn run and trusting the model not to work unguarded. But a
        refusal nobody can read is a dead end, so `landing status` can say what
        happened.
        """
        payload = json.dumps({"text": text, "at": time()}, indent=2)
        _atomic_write(self.refusal_path, payload + "\n")

    def load_refusal(self) -> str | None:
        if not self.refusal_path.exists():
            return None
        try:
            data = json.loads(self.refusal_path.read_text(encoding="utf-8"))
            if time() - float(data["at"]) > self.REFUSAL_TTL_SECONDS:
                return None
            return str(data["text"])
        except (ValueError, TypeError, KeyError, OSError):
            return None

    def clear_refusal(self) -> None:
        self.refusal_path.unlink(missing_ok=True)

    def save_session(self, session: TaskSession) -> None:
        _atomic_write(self.session_path, json.dumps(session.to_dict(), indent=2) + "\n")

    def load_session(self) -> TaskSession | None:
        if not self.session_path.exists():
            return None
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
            return TaskSession.from_dict(raw)
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            raise CorruptStateError(
                f"{self.session_path} exists but could not be read: {error}"
            ) from error

    def quarantine_session(self) -> Path | None:
        """Move an unreadable session aside so the guard has no mandate again.

        Kept rather than deleted: it is the only evidence of how the file was
        damaged, and an unreadable file has no burn history left to preserve.
        """
        if not self.session_path.exists():
            return None
        target = self.root / "session.corrupt.json"
        os.replace(self.session_path, target)
        return target

    def save_snapshot(self, snapshot: QuotaSnapshot) -> None:
        payload = snapshot.to_dict()
        _atomic_write(self.snapshot_path, json.dumps(payload, indent=2) + "\n")
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

        try:
            session = self.load_session()
        except CorruptStateError:
            # Ingesting quota must never fail: on Claude Code this runs as the
            # statusLine. Burn accounting is skipped, and the gate reports the
            # damaged session on its own terms.
            return
        if not session or session.provider != snapshot.provider:
            return
        window = snapshot.window(session.window)
        if not window:
            return
        remaining = window.remaining_percent
        previous = session.last_remaining_percent
        calls = session.tool_calls_since_snapshot
        if previous is None:
            session.last_remaining_percent = remaining
        else:
            burn = previous - remaining
            if burn < 0 or burn > 25.0:
                # Quota reset, increase, or an implausible jump: restart accounting
                # rather than feeding a garbage sample into the reserve estimate.
                session.tool_calls_since_snapshot = 0
                session.last_remaining_percent = remaining
            elif burn >= 0.01 and calls > 0:
                # Normalize to quota burned *per tool call*. Sampling per snapshot
                # interval would make the reserve drift with the host's refresh rate.
                session.burn_samples.append(round(burn / calls, 4))
                session.burn_samples = session.burn_samples[-24:]
                session.tool_calls_since_snapshot = 0
                session.last_remaining_percent = remaining
            # Otherwise the movement is too small to attribute yet: keep the same
            # baseline and keep counting calls so the eventual per-call rate is
            # divided by the true number of calls that produced it.
        self.save_session(session)

    def load_history(self, limit: int = 200) -> list[QuotaSnapshot]:
        if not self.history_path.exists():
            return []
        snapshots: list[QuotaSnapshot] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                snapshots.append(QuotaSnapshot.from_dict(json.loads(line)))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
        return snapshots

    def load_snapshot(self) -> QuotaSnapshot | None:
        if not self.snapshot_path.exists():
            return None
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            return QuotaSnapshot.from_dict(raw)
        except (ValueError, TypeError, KeyError, OSError):
            # An unreadable snapshot degrades to "no quota reading", which the
            # policy already handles as CONSERVE. Unknown is not 100%, so this is
            # the safe direction -- unlike the session, which carries the mandate.
            return None

    def snapshot_age_seconds(self) -> float | None:
        snapshot = self.load_snapshot()
        return None if not snapshot else max(0.0, time() - snapshot.sampled_at)
