from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_bridge.clock import IST
from agent_bridge.waits import ActiveWait, WaitSpec, begin_wait, check_wait, parse_wait_line, proc_start

NOW = datetime(2026, 10, 2, 22, 33, 0, tzinfo=IST)


@pytest.mark.parametrize(
    ("line", "spec"),
    [
        ("WAIT FOR PID 64410", WaitSpec("pid", "64410")),
        ("WAIT FOR PID 64410 MAX 6h", WaitSpec("pid", "64410", 21600)),
        ("WAIT FOR PID 64410 (max 90m)", WaitSpec("pid", "64410", 5400)),
        ("WAIT FOR FILE /tmp/fqa/resume2.done [MAX 2h]", WaitSpec("file", "/tmp/fqa/resume2.done", 7200)),
        ("WAIT FOR FILE results/run 1/done.json", WaitSpec("file", "results/run 1/done.json")),
        ("WAIT UNTIL 2026-10-03T02:41:00+05:30", WaitSpec("until", "2026-10-03T02:41:00+05:30")),
        ("WAIT UNTIL 2026-10-03T07:00:05Z", WaitSpec("until", "2026-10-03T07:00:05+00:00")),
        ("WAIT UNTIL 2026-10-03 02:41", WaitSpec("until", "2026-10-03T02:41:00+05:30")),
    ],
)
def test_parse_wait_line(line: str, spec: WaitSpec) -> None:
    assert parse_wait_line(line, now=NOW) == spec


@pytest.mark.parametrize("line", ["WAIT FOR PID abc", "WAIT UNTIL tomorrow", "WAIT FOR FILE ", "WAIT FOREVER", "WAIT FOR PID 5 MAX soon"])
def test_parse_wait_line_rejects(line: str) -> None:
    with pytest.raises(ValueError):
        parse_wait_line(line, now=NOW)


def test_describe_round_trips() -> None:
    spec = parse_wait_line("WAIT FOR PID 64410 MAX 6h", now=NOW)
    assert spec.describe() == "WAIT FOR PID 64410 MAX 6h"
    assert parse_wait_line(spec.describe(), now=NOW) == spec


def test_until_wait_finishes_at_the_target(tmp_path: Path) -> None:
    spec = parse_wait_line("WAIT UNTIL 2026-10-03T02:41:00+05:30", now=NOW)
    wait = begin_wait(spec, now=NOW, source="supervisor", wait_max=86400)
    assert wait.deadline == "2026-10-03T02:41:00+05:30" and not wait.clamped
    assert not check_wait(wait, now=NOW + timedelta(hours=4), repo=tmp_path).done
    result = check_wait(wait, now=datetime(2026, 10, 3, 2, 41, tzinfo=IST), repo=tmp_path)
    assert result.done and result.met


def test_until_beyond_the_max_is_clamped(tmp_path: Path) -> None:
    spec = parse_wait_line("WAIT UNTIL 2026-10-10T07:00:00+05:30", now=NOW)
    wait = begin_wait(spec, now=NOW, source="builder", wait_max=86400)
    assert wait.clamped and wait.deadline == (NOW + timedelta(days=1)).isoformat()
    result = check_wait(wait, now=NOW + timedelta(days=1), repo=tmp_path)
    assert result.done and not result.met and "MAX reached" in result.detail


def test_pid_wait_and_pid_reuse(tmp_path: Path) -> None:
    starts = {64410: "Fri Oct  2 22:30:00 2026"}
    spec = WaitSpec("pid", "64410")
    wait = begin_wait(spec, now=NOW, source="builder", wait_max=86400, pid_start=starts.get)
    assert wait.pid_start == "Fri Oct  2 22:30:00 2026"
    assert not check_wait(wait, now=NOW, repo=tmp_path, pid_start=starts.get).done
    starts[64410] = "Sat Oct  3 09:00:00 2026"  # a new process got the same pid
    result = check_wait(wait, now=NOW, repo=tmp_path, pid_start=starts.get)
    assert result.done and result.met and "exited" in result.detail
    del starts[64410]
    assert check_wait(wait, now=NOW, repo=tmp_path, pid_start=starts.get).met


def test_pid_not_running_at_start_is_met_at_once(tmp_path: Path) -> None:
    wait = begin_wait(WaitSpec("pid", "999999"), now=NOW, source="builder", wait_max=60, pid_start=lambda pid: None)
    result = check_wait(wait, now=NOW, repo=tmp_path, pid_start=lambda pid: None)
    assert result.met and "not running" in result.detail


def test_file_wait_relative_to_repo(tmp_path: Path) -> None:
    wait = begin_wait(WaitSpec("file", "results/done.json", 3600), now=NOW, source="supervisor", wait_max=86400)
    assert not check_wait(wait, now=NOW, repo=tmp_path).done
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "done.json").write_text("{}")
    assert check_wait(wait, now=NOW, repo=tmp_path).met


def test_active_wait_json_round_trip() -> None:
    wait = begin_wait(WaitSpec("pid", "7", 60), now=NOW, source="builder", wait_max=600, pid_start=lambda pid: "x")
    assert ActiveWait.from_json(wait.to_json()) == wait


def test_real_pid_start_time_for_this_process() -> None:
    import os

    from agent_bridge.waits import pid_start_time

    assert pid_start_time(os.getpid())
    assert pid_start_time(2**22 + 12345) is None


def test_proc_start_reads_field_22() -> None:
    stat = "1234 (my (odd) proc) S 1 1234 1234 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 98765 1000 100\n"
    assert proc_start(stat) == "boot+98765"
    assert proc_start(stat.replace(") S ", ") Z ")) is None
    assert proc_start("1234 (cut short) S 1 2") is None


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc")
def test_pid_start_time_needs_no_ps_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess
    import time

    from agent_bridge.waits import pid_start_time

    def no_ps(*args: object, **kw: object) -> None:
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", no_ps)
    assert pid_start_time(os.getpid()) and pid_start_time(os.getpid()) == pid_start_time(os.getpid())
    child = subprocess.Popen(["true"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not Path(f"/proc/{child.pid}/stat").read_text().split(") ")[1].startswith("Z"):
        time.sleep(0.01)
    assert pid_start_time(child.pid) is None
    child.wait()
