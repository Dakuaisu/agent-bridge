"""Owner actions run as agent-bridge CLI commands in their own session, so closing the UI never stops them."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_bridge.registry import state_home

KEEP = 200
HEADER = "$ agent-bridge "


def ui_dir() -> Path:
    d = state_home() / "ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def payload(label: str, text: str) -> str:
    """Long text reaches the CLI as @file: no quoting, no argv limits, and it stays for the record."""
    path = ui_dir() / f"{_stamp()}-{label}.md"
    path.write_text(text, encoding="utf-8")
    return f"@{path}"


@dataclass
class Job:
    label: str
    argv: list[str]
    cwd: Path
    log: Path
    proc: Any
    started: float
    then: Callable[[Job], None] | None = None
    code: int | None = None

    def output(self) -> str:
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(line for line in text.splitlines() if not line.startswith(HEADER))

    def tail(self, n: int = 2) -> list[str]:
        return [line.strip() for line in self.output().splitlines() if line.strip()][-n:]


class Runner:
    def __init__(self, popen: Callable[..., Any] = subprocess.Popen) -> None:
        self._popen = popen
        self.jobs: list[Job] = []

    def start(self, label: str, argv: list[str], *, cwd: Path, then: Callable[[Job], None] | None = None) -> Job:
        log = ui_dir() / f"{_stamp()}-{label}.log"
        with log.open("wb") as out:
            out.write(f"{HEADER}{' '.join(argv)}\n".encode())
            out.flush()
            proc = self._popen(
                [sys.executable, "-m", "agent_bridge", *argv],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=dict(os.environ),
            )
        job = Job(label, list(argv), Path(cwd), log, proc, time.monotonic(), then)
        self.jobs.append(job)
        self._prune()
        return job

    def poll(self) -> list[Job]:
        done = []
        for job in list(self.jobs):
            code = job.proc.poll()
            if code is not None:
                job.code = code
                self.jobs.remove(job)
                done.append(job)
        return done

    def busy(self, cwd: Path | None = None) -> list[Job]:
        return [j for j in self.jobs if cwd is None or j.cwd == cwd]

    def _prune(self) -> None:
        try:
            files = sorted(ui_dir().iterdir())
        except OSError:
            return
        for old in files[:-KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
