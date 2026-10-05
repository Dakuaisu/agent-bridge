"""The audit trail: events.jsonl (structured), loop.log (every message as delivered),
review.log (what the owner should look at), console.log (everything printed)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from agent_bridge.clock import Clock, iso
from agent_bridge.statedir import StateDir

RULE = "=" * 70


class Journal:
    def __init__(self, sd: StateDir, clock: Clock, *, echo: bool = True) -> None:
        self.sd = sd
        self.clock = clock
        self.echo = echo
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def _append(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(text)
            f.flush()

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": iso(self.clock.now()), "kind": kind, **fields}
        self._append(self.sd.events, json.dumps(record, ensure_ascii=False, default=str) + "\n")
        for listener in self._listeners:
            listener(record)
        return record

    def transcript(self, heading: str, text: str) -> None:
        """One message or reply in loop.log, exactly as delivered."""
        stamp = self.clock.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append(self.sd.loop_log, f"\n{RULE}\n{heading}  {stamp}\n{RULE}\n{text.rstrip()}\n")

    def review(self, title: str, body: str = "") -> None:
        stamp = self.clock.now().strftime("%Y-%m-%d %H:%M:%S")
        tail = f"{body.rstrip()}\n" if body.strip() else ""
        self._append(self.sd.review_log, f"\n=== {stamp} {title} ===\n{tail}")
        self.event("review", title=title, body=body)

    def console(self, line: str) -> None:
        self._append(self.sd.console_log, line.rstrip("\n") + "\n")
        if self.echo:
            print(line, file=sys.stdout, flush=True)

    def launch_marker(self, argv: list[str]) -> None:
        stamp = iso(self.clock.now())
        self._append(self.sd.console_log, f"\n=== launch {stamp}: agent-bridge {' '.join(argv)} ===\n")
        self.event("launch", argv=argv)


def read_events(path: Path) -> list[dict[str, Any]]:
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
