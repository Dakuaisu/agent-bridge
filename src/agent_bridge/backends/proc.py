"""Run an agent CLI as a streaming subprocess with a deadline, a cancel flag and a monitor hook.

The child gets its own process group, so a timeout or cancel ends it and everything it started.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from agent_bridge.backends.base import CancelToken


@dataclass
class StreamResult:
    returncode: int | None
    stderr: str
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    stalled_warned: bool = False


class MonitorStop(Exception):
    """Raised by a monitor to end the turn; the original error is carried in `error`."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def kill_group(proc: subprocess.Popen[str], grace: float = 10.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_streaming(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stdin_text: str | None,
    timeout: float,
    cancel: CancelToken,
    on_line: Callable[[str], None],
    monitor: Callable[[], None] | None = None,
    monitor_every: float = 15.0,
    stall_after: float = 1800.0,
    on_stall: Callable[[float], None] | None = None,
    poll: float = 0.25,
) -> StreamResult:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()
    err_chunks: list[str] = []

    def pump_out() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    def pump_err() -> None:
        assert proc.stderr is not None
        for chunk in proc.stderr:
            err_chunks.append(chunk)

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin_text or "")
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_err, daemon=True)]
    if stdin_text is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()

    result = StreamResult(returncode=None, stderr="")
    start = last_output = last_monitor = time.monotonic()
    done = False
    try:
        while not done:
            try:
                item = lines.get(timeout=poll)
            except queue.Empty:
                item = ""
            now = time.monotonic()
            if item is None:
                done = True
            elif item:
                last_output = now
                result.lines.append(item)
                on_line(item)
            if done:
                break
            if cancel.is_set():
                result.cancelled = True
                break
            if now - start > timeout:
                result.timed_out = True
                break
            if monitor is not None and now - last_monitor >= monitor_every:
                last_monitor = now
                monitor()
            if on_stall is not None and not result.stalled_warned and now - last_output > stall_after:
                result.stalled_warned = True
                on_stall(now - last_output)
    except MonitorStop:
        kill_group(proc)
        raise
    if result.cancelled or result.timed_out:
        kill_group(proc)
    else:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_group(proc)
    for t in threads[:2]:
        t.join(timeout=5)
    result.returncode = proc.returncode
    result.stderr = "".join(err_chunks)
    return result
