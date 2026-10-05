from __future__ import annotations

import json
from pathlib import Path

from agent_bridge.clock import FakeClock
from agent_bridge.journal import Journal, read_events
from agent_bridge.statedir import StateDir


def make(repo: Path, clock: FakeClock) -> tuple[StateDir, Journal]:
    sd = StateDir(repo)
    sd.ensure()
    return sd, Journal(sd, clock, echo=False)


def test_events_are_json_lines_with_origin_and_recipient(repo: Path, clock: FakeClock) -> None:
    sd, journal = make(repo, clock)
    seen: list[dict] = []
    journal.add_listener(seen.append)
    journal.event("message", origin="owner", recipient="builder", text="hello")
    clock.advance(5)
    journal.event("turn_end", role="builder", seconds=5)
    records = read_events(sd.events)
    assert [r["kind"] for r in records] == ["message", "turn_end"]
    assert records[0]["origin"] == "owner" and records[0]["recipient"] == "builder"
    assert records[0]["ts"] == "2026-10-06T12:00:00+05:30"
    assert records[1]["ts"] == "2026-10-06T12:00:05+05:30"
    assert seen == records
    for line in sd.events.read_text().splitlines():
        json.loads(line)


def test_transcript_keeps_the_message_exactly(repo: Path, clock: FakeClock) -> None:
    sd, journal = make(repo, clock)
    message = "[bridge] Headless run.\n[owner] (verbatim; binding)\nShip it.\n[supervisor]\n- first item"
    journal.transcript("EXCHANGE 3 | to builder", message)
    text = sd.loop_log.read_text()
    assert "EXCHANGE 3 | to builder  2026-10-06 12:00:00" in text
    assert message in text


def test_review_log_uses_the_old_marker_format(repo: Path, clock: FakeClock) -> None:
    sd, journal = make(repo, clock)
    journal.review("SUPERVISOR CHANGED THE REPO (session s1)", " M docs/PRD.md")
    journal.review("PROJECT COMPLETE at exchange 9")
    text = sd.review_log.read_text()
    assert "=== 2026-10-06 12:00:00 SUPERVISOR CHANGED THE REPO (session s1) ===\n M docs/PRD.md\n" in text
    assert "=== 2026-10-06 12:00:00 PROJECT COMPLETE at exchange 9 ===\n" in text
    assert [r["kind"] for r in read_events(sd.events)] == ["review", "review"]


def test_console_appends_with_launch_markers(repo: Path, clock: FakeClock) -> None:
    sd, journal = make(repo, clock)
    journal.launch_marker(["run", "--forever"])
    journal.console("first launch line")
    journal.launch_marker(["run", "--forever"])
    journal.console("second launch line")
    text = sd.console_log.read_text()
    assert text.count("=== launch 2026-10-06T12:00:00+05:30: agent-bridge run --forever ===") == 2
    assert text.index("first launch line") < text.index("second launch line")


def test_read_events_skips_damaged_lines(repo: Path, clock: FakeClock) -> None:
    sd, journal = make(repo, clock)
    journal.event("a")
    with sd.events.open("a") as f:
        f.write("{truncated\n\n")
    journal.event("b")
    assert [r["kind"] for r in read_events(sd.events)] == ["a", "b"]
    assert read_events(repo / "none.jsonl") == []
