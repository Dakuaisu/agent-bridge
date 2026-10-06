"""The .bridge/ folder: layout, atomic writes, the single-instance lock, the owner inbox, STOP."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _umask()


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file and rename. A symlink keeps pointing at its (rewritten) target, the file keeps its
    mode (new files get the umask's), and a file that used CRLF line endings keeps them."""
    path = Path(os.path.realpath(path)) if Path(path).is_symlink() else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = path.stat()
        mode = st.st_mode & 0o7777
        with path.open("rb") as existing:
            crlf = b"\r\n" in existing.read(65536)
    except FileNotFoundError:
        mode, crlf = 0o666 & ~_UMASK, False
    if crlf and "\r\n" not in text:
        text = text.replace("\n", "\r\n")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class LockHeld(Exception):
    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        pid = info.get("pid", "?")
        since = info.get("started", "?")
        super().__init__(f"another agent-bridge is running on this repo (pid {pid}, since {since})")


class BridgeLock:
    """flock on .bridge/lock. The OS drops it when the process dies, so a crash never leaves it stale."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, info: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockHeld(read_lock_info(self.path)) from None
        record = {"pid": os.getpid(), "host": socket.gethostname(), **info}
        os.ftruncate(fd, 0)
        os.write(fd, (json.dumps(record) + "\n").encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> BridgeLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def read_lock_info(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def lock_holder(path: Path) -> dict[str, Any] | None:
    """Who holds the lock, or None. Probes with a non-blocking flock; never blocks or steals."""
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_lock_info(path) or {"pid": "?"}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class InboxItem:
    path: Path
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.data.get("kind", ""))


class StateDir:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        self.root = self.repo / ".bridge"

    def ensure(self) -> None:
        for d in (self.root, self.plan, self.plan / "changes", self.turns, self.inbox, self.reports):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def lock(self) -> Path:
        return self.root / "lock"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def loop_log(self) -> Path:
        return self.root / "loop.log"

    @property
    def review_log(self) -> Path:
        return self.root / "review.log"

    @property
    def console_log(self) -> Path:
        return self.root / "console.log"

    @property
    def serve_log(self) -> Path:
        return self.root / "serve.log"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions.json"

    @property
    def plan(self) -> Path:
        return self.root / "plan"

    @property
    def turns(self) -> Path:
        return self.root / "turns"

    @property
    def builder_last(self) -> Path:
        return self.root / "builder_last.md"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    @property
    def stop_now_file(self) -> Path:
        return self.root / "STOP_NOW"

    @property
    def agent_pid(self) -> Path:
        return self.root / "agent.pid"

    @property
    def unsent(self) -> Path:
        return self.root / "unsent_reply.md"

    @property
    def paused(self) -> Path:
        return self.root / "PAUSED.md"

    @property
    def owner_todo(self) -> Path:
        return self.root / "owner_todo.md"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    # -- stop

    def stop_requested(self) -> bool:
        return self.stop_file.exists() or self.stop_now_file.exists()

    def stop_now_requested(self) -> bool:
        return self.stop_now_file.exists()

    def request_stop(self, now: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_file.touch()
        if now:
            self.stop_now_file.touch()

    def clear_stop(self) -> None:
        self.stop_file.unlink(missing_ok=True)
        self.stop_now_file.unlink(missing_ok=True)

    # -- owner inbox: say, approvals and decisions docs, queued for the running loop

    def inbox_put(self, kind: str, payload: dict[str, Any], stamp: str) -> Path:
        self.inbox.mkdir(parents=True, exist_ok=True)
        seq = len(list(self.inbox.glob("*.json")))
        path = self.inbox / f"{stamp.replace(':', '').replace('-', '')}-{os.getpid()}-{seq:03d}-{kind}.json"
        atomic_write_json(path, {"kind": kind, "queued": stamp, **payload})
        return path

    def inbox_peek(self) -> list[InboxItem]:
        """Queued items, oldest first. They stay queued until ack(), so a crash loses nothing."""
        items = []
        for p in sorted(self.inbox.glob("*.json")):
            data = read_json(p)
            if isinstance(data, dict):
                items.append(InboxItem(p, data))
        return items

    def inbox_set_aside(self, item: InboxItem, error: str) -> Path:
        """A queued item that failed: kept for the owner in inbox/failed/, never retried automatically."""
        failed = self.inbox / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        target = failed / item.path.name
        with contextlib.suppress(FileNotFoundError):
            os.replace(item.path, target)
        (failed / f"{item.path.stem}.error.txt").write_text(error + "\n", encoding="utf-8")
        return target

    def inbox_ack(self, item: InboxItem) -> None:
        done = self.inbox / "delivered"
        done.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.replace(item.path, done / item.path.name)

    def iter_turn_files(self) -> Iterator[Path]:
        return iter(sorted(self.turns.glob("*.jsonl")))
