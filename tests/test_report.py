from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_bridge.clock import IST
from agent_bridge.config import load_config
from agent_bridge.engine import State
from agent_bridge.report import github_base, parse_results, write_report
from agent_bridge.statedir import StateDir

from conftest import git
from harness import BASE_TOML

LEDGER = "# Decisions\n\n## DEC-001 Use SQLite\n- Decided by: planner\n- Status: AUTONOMOUS DECISION - owner to review\n\nWhy.\n"
OPEN = "# Open items\n\n## OPEN-001 Hand labels\n- Status: OWNER-BLOCKED\n\nOnly the owner.\n"
RESULTS = """# Results

| id | what | value | kind | run | commit |
|---|---|---|---|---|---|
| RES-1 | accuracy on fixtures | 0.91 | real | runs/a1 | abc1234 |
| RES-2 | accuracy, quick check | 0.88 | development | runs/d1 | def5678 |
| RES-3 | latency | 12 ms | maybe | runs/x | 0000000 |
"""


def setup(repo: Path) -> tuple:
    (repo / "bridge.toml").write_text(BASE_TOML)
    (repo / "docs").mkdir()
    (repo / "docs/DECISIONS.md").write_text(LEDGER)
    (repo / "docs/OPEN.md").write_text(OPEN)
    (repo / "docs/RESULTS.md").write_text(RESULTS)
    (repo / "docs/PRD.md").write_text("# PRD\n\n- R-1: one\n- R-2: print as JSON\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs")
    git(repo, "remote", "add", "origin", "git@github.com:someone/demo.git")
    cfg = load_config(repo / "bridge.toml")
    sd = StateDir(repo)
    sd.ensure()
    (sd.plan / "changes" / "PC-001.json").write_text(
        json.dumps(
            {
                "id": "PC-001",
                "title": "JSON output",
                "status": "applied",
                "affects": ["R-2"],
                "dec": "DEC-001",
                "dec_line": 3,
                "diff": ".bridge/plan/changes/PC-001.diff",
                "edits": [{"path": "docs/PRD.md", "find": "- R-2: print", "replace": "- R-2: print as JSON"}],
            }
        )
    )
    sd.review_log.write_text(
        "\n=== 2026-10-06 11:00:00 MODEL MISMATCH: before approval ===\n"
        "\n=== 2026-10-06 13:00:00 SUPERVISOR TURN CHANGED THE REPO (session s, exchange 2) ===\nx.md\n"
        "\n=== 2026-10-06 13:05:00 DANGER COMMAND (force push) at exchange 3 ===\ngit push --force\n"
    )
    state = State(
        phase="COMPLETE",
        exchange=9,
        contract={"approved_at": "2026-10-06T12:00:00+05:30", "by": "owner", "hashes": {}},
        phases_done=[{"phase": "Phase 1", "exchange": 4, "at": "2026-10-06T14:00:00+05:30", "dec": "DEC-002"}],
        complete={"at": "2026-10-06T18:00:00+05:30", "exchange": 9, "summary": "done"},
    )
    return cfg, sd, state


def test_report_sections_links_and_commits(repo: Path) -> None:
    cfg, sd, state = setup(repo)
    head = git(repo, "rev-parse", "HEAD").strip()
    path = write_report(cfg, sd, state, now=datetime(2026, 10, 6, 18, 5, tzinfo=IST))
    text = path.read_text()
    assert path.parent == sd.reports
    assert "- State: COMPLETE (PROJECT COMPLETE at exchange 9" in text
    assert "- Phase 1: verified complete by the supervisor at exchange 4" in text
    assert "- supervisor: claude-code claude-fable-5-1; read-only: enforced: --tools Read,Grep,Glob" in text
    assert f"**DEC-001** Use SQLite. Decided by: planner. [docs/DECISIONS.md:3](../../docs/DECISIONS.md#L3), commit [{head[:7]}](https://github.com/someone/demo/commit/{head})" in text
    assert "**OPEN-001** OPEN-001 Hand labels." in text and "[docs/OPEN.md:3](../../docs/OPEN.md#L3)" in text
    assert f"**PC-001** JSON output: applied; affects R-2; DEC-001 [docs/DECISIONS.md:3](../../docs/DECISIONS.md#L3)" in text
    assert f"commit [{head[:7]}](https://github.com/someone/demo/commit/{head})" in text.split("## 4.")[1]
    results = text.split("## 5.")[1]
    assert "### Real results (1)" in results and "RES-1: accuracy on fixtures = 0.91; run runs/a1; commit abc1234" in results
    assert "### Development results (never presented as real) (1)" in results and "RES-2" in results
    assert "### Rows with no valid kind (fix the register) (1)" in results and "RES-3" in results
    warnings = text.split("## 6.")[1]
    assert "MODEL MISMATCH: before approval" not in warnings
    assert "### Tripwire: a read-only role's turn changed the repo or used write tools (1)" in warnings
    assert "### Danger commands (1)" in warnings
    latest = sd.report.read_text()
    assert "(../docs/DECISIONS.md#L3)" in latest and "[.bridge/review.log](review.log)" in latest


def test_missing_results_register_is_stated(repo: Path) -> None:
    cfg, sd, state = setup(repo)
    cfg.project.results.unlink()
    text = write_report(cfg, sd, state, now=datetime(2026, 10, 6, 18, 5, tzinfo=IST)).read_text()
    assert "No results to report: there is no results register (RESULTS.md does not exist)." in text


def test_uncommitted_entries_say_so(repo: Path) -> None:
    cfg, sd, state = setup(repo)
    cfg.project.decisions.write_text(LEDGER + "\n## DEC-002 Later\n- Status: AUTONOMOUS DECISION - owner to review\n")
    text = write_report(cfg, sd, state, now=datetime(2026, 10, 6, 18, 5, tzinfo=IST)).read_text()
    assert "**DEC-002** Later." in text and "commit not committed yet" in text


def test_helpers() -> None:
    assert github_base("https://github.com/a/b.git") == "https://github.com/a/b"
    assert github_base("git@github.com:a/b") == "https://github.com/a/b"
    assert github_base("https://gitlab.com/a/b") is None and github_base(None) is None
    rows, missing = parse_results(Path("/nonexistent/RESULTS.md"))
    assert rows == [] and "does not exist" in missing
