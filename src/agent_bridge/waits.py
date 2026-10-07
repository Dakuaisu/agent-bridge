"""WAIT UNTIL / WAIT FOR PID / WAIT FOR FILE: sleep with no model calls (docs/DESIGN.md 7.1)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent_bridge.config import format_duration, parse_duration

_MAX_SUFFIX = re.compile(r"\s*(?:\[\s*MAX\s+([^\]]+)\]|\(\s*max\s+([^)]+)\)|\bMAX\s+(\S+))\s*$", re.IGNORECASE)
_UNTIL = re.compile(r"^WAIT\s+UNTIL\s+(.+)$")
_PID = re.compile(r"^WAIT\s+FOR\s+PID\s+(\d+)$")
_FILE = re.compile(r"^WAIT\s+FOR\s+FILE\s+(.+)$")


@dataclass(frozen=True)
class WaitSpec:
    kind: str  # until | pid | file
    target: str
    max_seconds: int | None = None

    def describe(self) -> str:
        head = {"until": "WAIT UNTIL", "pid": "WAIT FOR PID", "file": "WAIT FOR FILE"}[self.kind]
        tail = f" MAX {format_duration(self.max_seconds)}" if self.max_seconds else ""
        return f"{head} {self.target}{tail}"


def is_wait_line(line: str) -> bool:
    return line.startswith("WAIT UNTIL") or line.startswith("WAIT FOR")


def parse_wait_line(line: str, *, now: datetime) -> WaitSpec:
    """Parse one normalized directive line. Raises ValueError with a message for the agent."""
    text = line.strip()
    max_seconds = None
    m = _MAX_SUFFIX.search(text)
    if m:
        max_seconds = parse_duration(next(g for g in m.groups() if g).strip())
        text = text[: m.start()].rstrip()
    if m := _PID.match(text):
        return WaitSpec("pid", m.group(1), max_seconds)
    if m := _FILE.match(text):
        path = _first_path(m.group(1).strip())
        if not path:
            raise ValueError("WAIT FOR FILE needs a path")
        return WaitSpec("file", path, max_seconds)
    if m := _UNTIL.match(text):
        when = _parse_time(m.group(1).strip().strip("'\"`"), now)
        return WaitSpec("until", when.isoformat(timespec="seconds"), max_seconds)
    raise ValueError(f"not a WAIT directive: {line!r} (use WAIT UNTIL <ISO time>, WAIT FOR PID <n> or WAIT FOR FILE <path>)")


def _first_path(text: str) -> str:
    """The path, without a note after it: "(why)", ", then ..." or " - why". A quoted path is taken as written."""
    if text[:1] in ("'", '"', "`"):
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else text[1:]
    text = re.sub(r"\s+\(.*\)\s*$", "", text)
    text = re.split(r",\s|;\s|\s+(?:-{1,2}|—|–)\s+", text, maxsplit=1)[0]
    return text.strip().rstrip(",;.")


def _parse_time(text: str, now: datetime) -> datetime:
    try:
        when = datetime.fromisoformat(text.replace(" ", "T", 1) if " " in text and "T" not in text else text)
    except ValueError:
        raise ValueError(f"WAIT UNTIL needs an ISO-8601 time like 2026-10-03T02:41:00+05:30, got {text!r}") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    return when


PROC = Path("/proc")


def proc_start(stat: str) -> str | None:
    """The start time in /proc/<pid>/stat (field 22, clock ticks after boot), or None for a process that ended."""
    fields = stat[stat.rfind(")") + 2 :].split()
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return f"boot+{fields[19]}"


def pid_start_time(pid: int) -> str | None:
    """When the process started, or None if it is not running. Guards against pid reuse.

    Linux reads /proc, because minimal images have no ps; elsewhere ps answers."""
    if (PROC / "self" / "stat").exists():
        try:
            return proc_start((PROC / str(pid) / "stat").read_text())
        except (OSError, ValueError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    start = out.stdout.strip()
    return start or None


@dataclass
class ActiveWait:
    spec: WaitSpec
    started: str
    deadline: str
    source: str  # supervisor | builder
    clamped: bool = False
    pid_start: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["spec"] = asdict(self.spec)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ActiveWait:
        return cls(**{**data, "spec": WaitSpec(**data["spec"])})


@dataclass(frozen=True)
class WaitCheck:
    done: bool
    met: bool
    detail: str


def begin_wait(
    spec: WaitSpec,
    *,
    now: datetime,
    source: str,
    wait_max: int,
    pid_start: Callable[[int], str | None] = pid_start_time,
) -> ActiveWait:
    limit = wait_max if spec.max_seconds is None else min(spec.max_seconds, wait_max)
    clamped = spec.max_seconds is not None and spec.max_seconds > wait_max
    deadline = now + timedelta(seconds=limit)
    if spec.kind == "until":
        target = datetime.fromisoformat(spec.target)
        if target > deadline:
            clamped = True
        else:
            deadline = max(target, now)
    start = pid_start(int(spec.target)) if spec.kind == "pid" else None
    return ActiveWait(
        spec=spec,
        started=now.isoformat(timespec="seconds"),
        deadline=deadline.isoformat(timespec="seconds"),
        source=source,
        clamped=clamped,
        pid_start=start,
    )


def check_wait(
    wait: ActiveWait,
    *,
    now: datetime,
    repo: Path,
    pid_start: Callable[[int], str | None] = pid_start_time,
) -> WaitCheck:
    spec = wait.spec
    if spec.kind == "until":
        if now >= datetime.fromisoformat(spec.target):
            return WaitCheck(True, True, f"the time {spec.target} was reached")
    elif spec.kind == "pid":
        if wait.pid_start is None:
            return WaitCheck(True, True, f"process {spec.target} was not running when the wait began")
        current = pid_start(int(spec.target))
        if current is None or current != wait.pid_start:
            return WaitCheck(True, True, f"process {spec.target} exited")
    elif spec.kind == "file":
        path = Path(spec.target)
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return WaitCheck(True, True, f"{path} exists")
    if now >= datetime.fromisoformat(wait.deadline):
        return WaitCheck(True, False, f"MAX reached at {wait.deadline} without the condition")
    return WaitCheck(False, False, "waiting")
