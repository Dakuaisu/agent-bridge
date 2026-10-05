from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bridge.statedir import (
    BridgeLock,
    LockHeld,
    StateDir,
    atomic_write_json,
    atomic_write_text,
    lock_holder,
    read_json,
)


def test_atomic_writes_leave_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "state.json"
    atomic_write_json(target, {"a": 1})
    atomic_write_text(tmp_path / "sub" / "x.md", "hello")
    assert read_json(target) == {"a": 1}
    assert sorted(p.name for p in target.parent.iterdir()) == ["state.json", "x.md"]
    assert read_json(tmp_path / "missing.json", default={"d": 0}) == {"d": 0}


def test_layout_uses_the_old_file_names(repo: Path) -> None:
    sd = StateDir(repo)
    sd.ensure()
    assert sd.root == repo / ".bridge"
    names = {p.name for p in (sd.loop_log, sd.review_log, sd.console_log, sd.serve_log, sd.unsent, sd.builder_last, sd.stop_file)}
    assert names == {"loop.log", "review.log", "console.log", "serve.log", "unsent_reply.md", "builder_last.md", "STOP"}
    for d in (sd.plan, sd.turns, sd.inbox, sd.reports):
        assert d.is_dir()


def test_stop_flags(repo: Path) -> None:
    sd = StateDir(repo)
    assert not sd.stop_requested()
    sd.request_stop()
    assert sd.stop_requested() and not sd.stop_now_requested()
    sd.request_stop(now=True)
    assert sd.stop_now_requested()
    sd.clear_stop()
    assert not sd.stop_requested()


def test_inbox_keeps_items_until_acknowledged(repo: Path) -> None:
    sd = StateDir(repo)
    sd.ensure()
    sd.inbox_put("say", {"text": "first"}, "2026-10-06T12:00:00+05:30")
    sd.inbox_put("approve", {"ids": ["PC-1"]}, "2026-10-06T12:00:01+05:30")
    items = sd.inbox_peek()
    assert [i.kind for i in items] == ["say", "approve"]
    assert items[0].data["text"] == "first"
    assert sd.inbox_peek() == items
    sd.inbox_ack(items[0])
    assert [i.kind for i in sd.inbox_peek()] == ["approve"]
    assert (sd.inbox / "delivered" / items[0].path.name).exists()


def test_lock_is_exclusive_across_processes(repo: Path) -> None:
    sd = StateDir(repo)
    lock = BridgeLock(sd.lock)
    lock.acquire({"started": "2026-10-06T12:00:00+05:30", "argv": ["run"]})
    try:
        holder = lock_holder(sd.lock)
        assert holder is not None and holder["argv"] == ["run"]
        probe = (
            "import sys\n"
            "from agent_bridge.statedir import BridgeLock, LockHeld\n"
            "try:\n"
            f"    BridgeLock(__import__('pathlib').Path({str(sd.lock)!r})).acquire({{}})\n"
            "except LockHeld as e:\n"
            "    print('held', e.info.get('argv'))\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert "held ['run']" in out.stdout
    finally:
        lock.release()
    assert lock_holder(sd.lock) is None
    second = BridgeLock(sd.lock)
    second.acquire({"argv": ["again"]})
    second.release()


def test_lock_released_when_the_holder_dies(repo: Path) -> None:
    sd = StateDir(repo)
    sd.ensure()
    script = (
        "import os\n"
        "from pathlib import Path\n"
        "from agent_bridge.statedir import BridgeLock\n"
        f"BridgeLock(Path({str(sd.lock)!r})).acquire({{'argv': ['crash']}})\n"
        "os._exit(9)\n"
    )
    subprocess.run([sys.executable, "-c", script], check=False)
    assert lock_holder(sd.lock) is None
    with BridgeLock(sd.lock) as lock:
        lock.acquire({})
        assert lock.held


def test_lock_held_message_names_the_pid(repo: Path) -> None:
    err = LockHeld({"pid": 4242, "started": "2026-10-06T12:00:00"})
    assert "pid 4242" in str(err)
    assert json.loads(json.dumps(err.info))["pid"] == 4242


@pytest.mark.parametrize("bad", ["", "{not json"])
def test_unreadable_lock_info_is_tolerated(repo: Path, bad: str) -> None:
    sd = StateDir(repo)
    sd.ensure()
    sd.lock.write_text(bad)
    assert lock_holder(sd.lock) is None
