from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import cli, runtime
from agent_bridge.backends.base import READ_ONLY_BY_INSTRUCTION
from agent_bridge.backends.fake import FakeBackend, FakeScriptExhausted
from agent_bridge.clock import FakeClock
from agent_bridge.config import ROLES, load_config
from agent_bridge.engine import State

from fixtures import DECISIONS, OPEN, PRD, QUESTIONS, RULES, plan_text
from harness import DONE, ok


class Fakes:
    """Scripts per role; the CLI builds its backends through runtime's factory."""

    def __init__(self) -> None:
        self.scripts: dict[str, list[Any]] = {r: [] for r in ROLES}
        self.read_only: dict[str, str] = {}
        self.backends: dict[str, FakeBackend] = {}
        self.directory: str | None = None

    def _next(self, role: str):
        def script(message: str, backend: FakeBackend):
            if not self.scripts[role]:
                raise FakeScriptExhausted(f"{role}: no scripted reply left for:\n{message}")
            return self.scripts[role].pop(0)

        return script

    def factory(self, cfg, sd):
        out = {}
        for role in ROLES:
            kw = {"read_only": self.read_only[role]} if role in self.read_only else {}
            b = FakeBackend(cfg.role(role), repo=cfg.project.repo, project=cfg.project.name, clock=FakeClock(), script=self._next(role), **kw)
            b.session_directory = lambda sid, d=self.directory: d
            out[role] = b
        self.backends = out
        return out


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    f = Fakes()
    monkeypatch.setattr(runtime, "_factory", f.factory)
    monkeypatch.setattr(runtime, "start_caffeinate", lambda enabled: None)
    monkeypatch.setattr(runtime, "version_warning", lambda binary, accept: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return f


def run(*args: str) -> int:
    return cli.main([*args])


def contract_files(repo: Path) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/PRD.md").write_text(PRD)
    (repo / "CLAUDE.md").write_text(RULES)
    (repo / "docs/DECISIONS.md").write_text(DECISIONS)
    (repo / "docs/OPEN.md").write_text(OPEN)


def test_init(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "docs").mkdir()
    (repo / "docs/TRADEOFFS.md").write_text("# Tradeoffs\n")
    (repo / "docs/WORKLOG.md").write_text("# Worklog\n")
    assert run("init", "--repo", str(repo)) == 0
    cfg = load_config(repo / "bridge.toml")
    assert cfg.project.decisions.name == "TRADEOFFS.md" and cfg.project.worklog.name == "WORKLOG.md"
    assert ".bridge/" in (repo / ".gitignore").read_text()
    assert "ln -s CLAUDE.md AGENTS.md" in capsys.readouterr().out
    assert run("init", "--repo", str(repo)) == 2


def test_init_with_opencode_picks_a_port_and_adopts_legacy_state(repo: Path, fakes: Fakes) -> None:
    (repo / "CLAUDE.md").write_text(RULES)
    legacy = repo / ".bridge"
    legacy.mkdir()
    (legacy / "session").write_text("ses_builder1\n")
    (legacy / "supervisor_session").write_text("ses_super1")
    (legacy / "builder_last.md").write_text("Phase 2 complete; DECISIONS NEEDED: none")
    code = run("init", "--repo", str(repo), "--builder", "opencode:anthropic/claude-opus-5-5", "--supervisor", "opencode:anthropic/claude-fable-5-1", "--write-rules", "--adopt-legacy")
    assert code == 0
    cfg = load_config(repo / "bridge.toml")
    assert 4100 <= cfg.opencode.port < 4200 and cfg.role("builder").engine == "opencode"
    assert "## Running under agent-bridge" in (repo / "CLAUDE.md").read_text()
    st = State.load(legacy / "state.json")
    assert st.sessions["builder"]["id"] == "ses_builder1" and st.sessions["supervisor"]["id"] == "ses_super1"
    assert st.phase == "SUPERVISOR_TURN" and "carried over from the old bridge" in st.review["notes"][0]


def test_new_with_auto_approve_runs_to_completion_and_writes_the_report(tmp_path: Path, fakes: Fakes) -> None:
    project = tmp_path / "proj"
    fakes.scripts["planner"] = [QUESTIONS, plan_text(toml_extra="[safety]\ncaffeinate = false\n")]
    fakes.scripts["builder"] = ["Committed the contract; Phase 1 done.", "more"]
    fakes.scripts["supervisor"] = [ok("next"), DONE]
    assert run("new", "A tiny CLI.", "--repo", str(project), "--auto-approve") == 0
    assert (project / ".git").exists() and (project / "docs/PRD.md").exists()
    st = State.load(project / ".bridge/state.json")
    assert st.phase == "COMPLETE" and st.contract["auto_approve"] is True
    report = (project / ".bridge/report.md").read_text()
    assert "# Owner-review report: demo" in report and "## 2. Decisions to review (3)" in report
    assert "=== launch" in (project / ".bridge/console.log").read_text()


def test_new_without_a_terminal_stops_at_the_interview_then_say_answers(tmp_path: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    fakes.scripts["planner"] = [QUESTIONS, plan_text()]
    assert run("new", "A tiny CLI.", "--repo", str(project)) == 3
    assert "The planner asks:" in capsys.readouterr().out
    assert State.load(project / ".bridge/state.json").phase == "INTERVIEW"
    assert run("say", "use your recommendations", "--repo", str(project)) == 3
    assert State.load(project / ".bridge/state.json").phase == "PLAN_REVIEW"
    assert "[owner] (verbatim; the owner's answers to your questions)\nuse your recommendations" in fakes.backends["planner"].sent[-1]


def test_new_refuses_an_existing_contract(repo: Path, fakes: Fakes) -> None:
    (repo / "CLAUDE.md").write_text("x")
    assert run("new", "idea", "--repo", str(repo)) == 2


def test_approve_adopts_then_run_kickoff(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    contract_files(repo)
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\n[safety]\ncaffeinate = false\n")
    assert run("run", "--repo", str(repo), "--kickoff", "go") == 2
    assert "No approved contract" in capsys.readouterr().out
    assert run("approve", "--repo", str(repo)) == 0
    fakes.scripts["builder"] = ["did it", "x"]
    fakes.scripts["supervisor"] = [ok("next"), DONE]
    assert run("run", "--repo", str(repo), "--kickoff", "Continue Phase 1.", "--forever") == 0
    assert State.load(repo / ".bridge/state.json").phase == "COMPLETE"
    assert "[owner] (verbatim; binding)\nContinue Phase 1." in fakes.backends["builder"].sent[0]
    out = capsys.readouterr().out
    assert "supervisor claude-code" not in out or "read-only:" in out


def test_status_text_and_json(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    contract_files(repo)
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\n[safety]\ncaffeinate = false\n")
    fakes.read_only["supervisor"] = READ_ONLY_BY_INSTRUCTION
    run("approve", "--repo", str(repo))
    fakes.scripts["builder"] = ["r0", "r1"]
    fakes.scripts["supervisor"] = [ok("go"), DONE]
    run("run", "--repo", str(repo), "--kickoff", "go", "--forever")
    capsys.readouterr()
    assert run("status", "--repo", str(repo)) == 0
    text = capsys.readouterr().out
    assert "state:        COMPLETE (exchange 2)" in text
    assert "supervisor:   fake claude-fable-5-1; read-only: read-only by instruction only" in text
    assert "running:      no" in text
    assert run("status", "--repo", str(repo), "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["phase"] == "COMPLETE" and data["roles"]["planner"]["read_only"].startswith("enforced")


def test_say_and_stop_without_a_running_bridge(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bridge.toml").write_text("version = 1\n")
    assert run("say", "Prefer SQLite.", "--repo", str(repo), "--to", "supervisor") == 0
    items = list((repo / ".bridge/inbox").glob("*.json"))
    assert len(items) == 1 and json.loads(items[0].read_text())["to"] == "supervisor"
    assert run("stop", "--repo", str(repo)) == 0
    assert "no bridge is running" in capsys.readouterr().out
    assert not (repo / ".bridge/STOP").exists()


def test_stop_and_approve_while_a_bridge_holds_the_lock(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bridge.toml").write_text("version = 1\n")
    (repo / ".bridge").mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c", f"from pathlib import Path; from agent_bridge.statedir import BridgeLock; import time; BridgeLock(Path({str(repo / '.bridge/lock')!r})).acquire({{'argv': ['run']}}); print('locked', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert run("stop", "--repo", str(repo), "--now") == 0
        assert (repo / ".bridge/STOP").exists() and (repo / ".bridge/STOP_NOW").exists()
        assert run("approve", "PC-001", "--repo", str(repo)) == 0
        assert "queued for the running bridge" in capsys.readouterr().out
        assert run("pin", "--builder", "ses_x", "--repo", str(repo)) == 2
    finally:
        holder.kill()
        holder.wait()


def test_review_and_template(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    contract_files(repo)
    (repo / "docs/DECISIONS.md").write_text("# D\n\n## DEC-001 Use SQLite\n- Decided by: planner\n- Status: AUTONOMOUS DECISION - owner to review\n")
    (repo / "bridge.toml").write_text("version = 1\n")
    assert run("review", "--repo", str(repo), "--template", "docs/OWNER_REVIEW.md") == 0
    out = capsys.readouterr().out
    assert "- [ ] DEC-001 Use SQLite" in out and "- [ ] OPEN-001 OPEN-001 Real fixture data" in out
    assert "## D1 — Use SQLite" in (repo / "docs/OWNER_REVIEW.md").read_text()
    assert (repo / ".bridge/owner_todo.md").exists()
    assert run("review", "--repo", str(repo), "--template", "docs/OWNER_REVIEW.md") == 2


def test_pin_checks_the_session_directory(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bridge.toml").write_text("version = 1\n")
    fakes.directory = "/Users/someone/OtherProject"
    assert run("pin", "--builder", "ses_other", "--repo", str(repo)) == 2
    fakes.directory = str(repo.resolve())
    assert run("pin", "--builder", "ses_mine", "--repo", str(repo)) == 0
    assert State.load(repo / ".bridge/state.json").sessions["builder"]["id"] == "ses_mine"
    fakes.directory = None
    assert run("pin", "--supervisor", "unknown", "--repo", str(repo)) == 2
    assert run("pin", "--supervisor", "unknown", "--force", "--repo", str(repo)) == 0
    assert run("pin", "--new-builder", "--repo", str(repo)) == 0
    assert State.load(repo / ".bridge/state.json").sessions["builder"]["closed"] is True


def test_logs_report_and_check(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    contract_files(repo)
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\n[safety]\ncaffeinate = false\n")
    run("approve", "--repo", str(repo))
    fakes.scripts["builder"] = ["r0", "r1"]
    fakes.scripts["supervisor"] = [ok("go"), DONE]
    run("run", "--repo", str(repo), "--kickoff", "go", "--forever")
    capsys.readouterr()
    assert run("logs", "--repo", str(repo)) == 0
    logs = capsys.readouterr().out
    assert "== exchange 0 == builder" in logs and "supervisor VERDICT: sound" in logs and "PROJECT COMPLETE at exchange 2" in logs
    assert run("logs", "--repo", str(repo), "--transcript", "-n", "5") == 0
    assert run("report", "--repo", str(repo), "--out", str(repo / "R.md")) == 0
    assert "# Owner-review report: demo" in (repo / "R.md").read_text()
    capsys.readouterr()
    assert run("check", "--repo", str(repo)) == 0
    check = capsys.readouterr().out
    assert "supervisor:   fake claude-fable-5-1 (fake 1.0); read-only: enforced: --tools Read,Grep,Glob" in check
    assert "contract:     approved" in check and "git identity: Test Owner <owner@example.invalid>" in check


def test_bad_config_is_a_usage_error(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bridge.toml").write_text("version = 1\n[builder]\nengnie = 'x'\n")
    assert run("status", "--repo", str(repo)) == 2
    assert "builder.engnie: unknown key" in capsys.readouterr().err


def test_approve_refuses_while_planning_is_unfinished(tmp_path: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    fakes.scripts["planner"] = [QUESTIONS]
    assert run("new", "idea", "--repo", str(project)) == 3
    assert run("approve", "--repo", str(project)) == 2
    assert "the planner has not finished (INTERVIEW)" in capsys.readouterr().err
    assert State.load(project / ".bridge/state.json").contract is None


def test_legacy_import_skips_an_unsent_reply_the_old_bridge_ran_past(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    (repo / "CLAUDE.md").write_text(RULES)
    legacy = repo / ".bridge"
    legacy.mkdir()
    (legacy / "unsent_reply.md").write_text("Noted. Wait.")
    (legacy / "builder_last.md").write_text("Phase 5 built up to the owner-blocked items.")
    os.utime(legacy / "unsent_reply.md", (1_000_000, 1_000_000))
    assert run("init", "--repo", str(repo), "--adopt-legacy") == 0
    out = capsys.readouterr().out
    assert "older than builder_last.md" in out and "not delivered" in out
    st = State.load(legacy / "state.json")
    assert st.pending is None and st.phase == "SUPERVISOR_TURN"


def test_legacy_import_delivers_an_unsent_reply_newer_than_the_last_report(tmp_path: Path, fakes: Fakes) -> None:
    import os

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    legacy = repo / ".bridge"
    legacy.mkdir()
    (legacy / "builder_last.md").write_text("old report")
    os.utime(legacy / "builder_last.md", (1_000_000, 1_000_000))
    (legacy / "unsent_reply.md").write_text("Run the eval next.")
    assert run("init", "--repo", str(repo), "--adopt-legacy") == 0
    st = State.load(legacy / "state.json")
    assert st.phase == "BUILDER_TURN" and st.pending["supervisor"] == "Run the eval next."


def test_init_uses_supervisor_rules_and_check_warns_when_legacy_rules_are_missing(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    contract_files(repo)
    (repo / "tools").mkdir()
    (repo / "tools/bridge.py").write_text("SYSTEM = 'old rules'\n")
    assert run("init", "--repo", str(repo)) == 0
    assert "legacy: the old tools/bridge.py kept project rules" in capsys.readouterr().out
    assert load_config(repo / "bridge.toml").project.supervisor_rules is None
    assert run("check", "--repo", str(repo)) == 0
    assert "no project.supervisor_rules" in capsys.readouterr().out

    (repo / "bridge.toml").unlink()
    (repo / "docs/SUPERVISOR.md").write_text("Hand-written eval items are the owner's.\n")
    assert run("init", "--repo", str(repo)) == 0
    assert "supervisor  docs/SUPERVISOR.md: found" in capsys.readouterr().out
    assert load_config(repo / "bridge.toml").project.supervisor_rules == repo / "docs/SUPERVISOR.md"
    assert run("check", "--repo", str(repo)) == 0
    assert "supervisor_rules" not in capsys.readouterr().out

    (repo / "docs/SUPERVISOR.md").unlink()
    assert run("check", "--repo", str(repo)) == 0
    assert "does not exist; the supervisor runs without project rules" in capsys.readouterr().out


def test_approve_done_records_phases_finished_before_agent_bridge(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_bridge.statedir import BridgeLock

    contract_files(repo)
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\n[safety]\ncaffeinate = false\n")
    assert run("approve", "--done", "1", "--repo", str(repo)) == 2
    assert "no approved contract" in capsys.readouterr().err
    assert run("approve", "--no-run", "--repo", str(repo)) == 0
    assert run("approve", "--done", "7", "--repo", str(repo)) == 2
    assert "'7' is not a phase heading" in capsys.readouterr().err
    assert run("approve", "--done", "1", "--done", "Phase 1", "--reason", "WORKLOG: exit accepted at abc1234", "--repo", str(repo)) == 0
    assert "recorded Phase 1 - Parser as complete (DEC-011" in capsys.readouterr().out
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "## DEC-011 Phase 1 - Parser complete before agent-bridge" in ledger
    assert "- Status: OWNER DECISION (agent-bridge approve --done)" in ledger and "Evidence given by the owner: WORKLOG: exit accepted at abc1234" in ledger
    st = State.load(repo / ".bridge/state.json")
    assert [(d["phase"], d["by"]) for d in st.phases_done] == [("Phase 1 - Parser", "owner")]
    assert run("approve", "--done", "Phase 1 - Parser", "--repo", str(repo)) == 0
    assert "already recorded" in capsys.readouterr().out and len(State.load(repo / ".bridge/state.json").phases_done) == 1
    assert "PHASE RECORDED AS COMPLETE: Phase 1 - Parser" in (repo / ".bridge/review.log").read_text()
    lock = BridgeLock(repo / ".bridge/lock")
    lock.acquire({"argv": ["run"]})
    try:
        assert run("approve", "--done", "2", "--repo", str(repo)) == 2
        assert "stop it before recording phases" in capsys.readouterr().err
    finally:
        lock.release()
