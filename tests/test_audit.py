"""Regression tests for the 2026-10-06 audit: each finding, reproduced, then fixed."""

from __future__ import annotations

import http.client
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge import contract, prompts, runtime, sandbox
from agent_bridge.backends.base import NeverCancel, Reply, SessionLimit, ToolCall, Unsupported
from agent_bridge.backends.claude_code import ClaudeCodeBackend, _StreamParser, child_env
from agent_bridge.backends.fake import FakeStep
from agent_bridge.backends.opencode import OpencodeBackend, OpencodeDB
from agent_bridge.backends.proc import run_streaming
from agent_bridge.clock import FakeClock
from agent_bridge.config import RoleConfig, load_config
from agent_bridge.engine import Engine, State
from agent_bridge.journal import read_events
from agent_bridge.limits import classify
from agent_bridge.planner import ContractEngine
from agent_bridge.protocol import Block, Edit, parse_builder, parse_supervisor
from agent_bridge.statedir import StateDir, atomic_write_json, atomic_write_text
from agent_bridge.waits import parse_wait_line, pid_start_time

from fakebin import claude_turn, install, scenario
from fixtures import DECISIONS, OPEN, PRD, RULES, change
from harness import DONE, make_engine, ok, review_log
from test_cli import Fakes, contract_files, fakes, run  # noqa: F401  (fixture re-export)

REPLAN = "VERDICT: blocked\nSCOPE: none\nREPLAN (Phase 1): the plan is wrong\nREPLY:\nhold"


def adopt(repo: Path, clock: FakeClock, **kw) -> ContractEngine:
    (repo / "docs").mkdir(exist_ok=True)
    for p, t in (("docs/PRD.md", PRD), ("CLAUDE.md", RULES), ("docs/DECISIONS.md", DECISIONS), ("docs/OPEN.md", OPEN)):
        (repo / p).write_text(t)
    eng = make_engine(repo, clock, engine_cls=ContractEngine, **kw)
    eng.approve_plan(adopt=True)
    return eng


# ----------------------------------------------------------------- 1. agent processes outlive the bridge


def test_an_interrupt_ends_the_agent_and_everything_it_started(tmp_path: Path) -> None:
    marker, background = tmp_path / "marker", tmp_path / "background"
    started = time.monotonic()

    def monitor() -> None:
        if time.monotonic() - started > 0.5:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_streaming(
            ["sh", "-c", f"(sleep 2; touch {background}) & sleep 2; touch {marker}"],
            cwd=str(tmp_path),
            env={"PATH": "/bin:/usr/bin"},
            stdin_text=None,
            timeout=60,
            cancel=NeverCancel(),
            on_line=lambda line: None,
            monitor=monitor,
            monitor_every=0.1,
        )
    time.sleep(2.6)
    assert not marker.exists() and not background.exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_sighup_end_a_run_like_ctrl_c(tmp_path: Path, sig: signal.Signals) -> None:
    marker = tmp_path / "marker"
    script = tmp_path / "victim.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            from agent_bridge import runtime
            from agent_bridge.backends.base import NeverCancel
            from agent_bridge.backends.proc import run_streaming
            runtime.interrupt_on_signals()
            print("ready", flush=True)
            try:
                run_streaming(["sh", "-c", "sleep 2; touch {marker}"], cwd="/tmp", env={{"PATH": "/bin:/usr/bin"}},
                              stdin_text=None, timeout=60, cancel=NeverCancel(), on_line=lambda line: None)
            except KeyboardInterrupt:
                sys.exit(130)
            """
        )
    )
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout and proc.stdout.readline().strip() == "ready"
    time.sleep(0.4)
    proc.send_signal(sig)
    assert proc.wait(timeout=15) == 130
    time.sleep(2.2)
    assert not marker.exists()


def test_a_leftover_agent_from_a_killed_bridge_is_ended_before_the_resend(repo: Path, clock: FakeClock) -> None:
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
        atomic_write_json(eng.sd.agent_pid, {"pid": victim.pid, "start": pid_start_time(victim.pid)})
        eng.kickoff("go")
        eng.run()
        assert victim.wait(timeout=15) is not None
        assert "LEFTOVER AGENT PROCESS ENDED" in review_log(repo) and not eng.sd.agent_pid.exists()
    finally:
        if victim.poll() is None:
            victim.kill()


def test_a_stale_agent_record_never_kills_an_unrelated_process(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    atomic_write_json(eng.sd.agent_pid, {"pid": os.getpid(), "start": "Mon Jan  1 00:00:00 2001"})
    eng.kickoff("go")
    eng.run()
    assert "LEFTOVER" not in review_log(repo) and not eng.sd.agent_pid.exists()


def test_the_engine_records_the_running_agent(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0"], supervisor=[DONE])
    assert eng.backends["builder"].on_process == eng._track_agent
    eng._track_agent(SimpleNamespace(pid=os.getpid()))
    assert json.loads(eng.sd.agent_pid.read_text())["pid"] == os.getpid()
    eng._track_agent(None)
    assert not eng.sd.agent_pid.exists()


def test_claude_code_abort_ends_its_process(repo: Path) -> None:
    (repo / "bridge.toml").write_text("version = 1\n")
    b = ClaudeCodeBackend(load_config(repo / "bridge.toml").role("builder"), repo=repo, project="demo")
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    b._proc = proc  # type: ignore[assignment]
    b.abort()
    assert proc.wait(timeout=15) is not None


def test_an_interrupt_during_a_turn_aborts_it_and_stays_resumable(repo: Path, clock: FakeClock) -> None:
    def boom() -> None:
        raise KeyboardInterrupt

    eng = make_engine(repo, clock, builder=[FakeStep(text="x", action=boom)], supervisor=[])
    eng.kickoff("go")
    with pytest.raises(KeyboardInterrupt):
        eng.run()
    assert eng.backends["builder"].aborts == 1
    assert State.load(repo / ".bridge/state.json").pending["in_flight"] is True


# ----------------------------------------------------------------- 2, 3. what the planner may change


def test_a_replan_cannot_change_owner_only_settings(repo: Path, clock: FakeClock) -> None:
    edit = change([("bridge.toml", "name = 'demo'", "name = 'demo'\n[git]\npush = 'allowed'\n[billing]\nmode = 'api-key'")], material="no")
    eng = adopt(repo, clock, planner=[edit, edit], builder=["r1", "r2"], supervisor=[REPLAN, ok("go on"), DONE])
    eng.kickoff("go")
    eng.run()
    cfg = load_config(repo / "bridge.toml")
    assert cfg.git_push == "never" and cfg.billing_mode == "subscription"
    log = review_log(repo)
    assert "RE-PLAN FAILED THE CHECKS TWICE" in log and "owner's to change" in log
    assert not contract.drifted(cfg, State.load(repo / ".bridge/state.json").contract["hashes"])


def test_a_replan_that_touches_bridge_toml_waits_for_the_owner(repo: Path, clock: FakeClock) -> None:
    edit = change([("bridge.toml", "name = 'demo'", "name = 'demo'\n[budget]\nmax_exchanges = 9")], material="no")
    eng = adopt(repo, clock, planner=[edit], builder=["r1", "r2"], supervisor=[REPLAN, ok("go on"), DONE])
    eng.kickoff("go")
    eng.run()
    record = json.loads((repo / ".bridge/plan/changes/PC-001.json").read_text())
    assert record["status"] == "awaiting" and record["material"] is True
    assert load_config(repo / "bridge.toml").budget.max_exchanges is None


def test_a_replan_cannot_rewrite_who_decided_what(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/DECISIONS.md", "- Status: OWNER DECISION (agent-bridge approve)", "- Status: REJECTED by the owner: superseded")], material="no")
    eng = adopt(repo, clock, planner=[edit, edit], builder=["r1", "r2"], supervisor=[REPLAN, ok("go on"), DONE])
    eng.kickoff("go")
    eng.run()
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "REJECTED by the owner: superseded" not in ledger and "- Status: OWNER DECISION (agent-bridge approve)" in ledger
    assert "may add entries, but not change or remove" in review_log(repo)


def test_protected_ledger_lines() -> None:
    ok_add = contract.protected_line_errors("L", "## A\n- Status: OPEN\n", "## A\n- Status: OPEN\n## B\n- Status: PROPOSED\n", may_change_status=False)
    assert ok_add == []
    assert contract.protected_line_errors("L", "- Status: OWNER-BLOCKED\n", "- Status: RESOLVED\n", may_change_status=False)
    assert contract.protected_line_errors("L", "- Status: OWNER-BLOCKED\n", "- Status: RESOLVED (D1)\n", may_change_status=True) == []
    claim = contract.protected_line_errors("L", "", "- Status: APPROVED by the owner\n", may_change_status=True)
    assert claim and "cannot claim the owner's decision" in claim[0]


# ----------------------------------------------------------------- 4. one bad input stops everything


def test_a_backslash_in_a_rejection_reason_is_kept_literally(repo: Path, clock: FakeClock) -> None:
    material = change([("docs/PRD.md", "- R-2: print a summary.", "- R-2: print a JSON summary.")], material="yes", affects="R-2")
    eng = adopt(repo, clock, planner=[material], builder=["r1", "r2", "r3"], supervisor=[REPLAN, ok("go", scope="R-1"), ok("go", scope="R-1")])
    eng.kickoff("go")
    eng.run(exchanges=1)
    eng.sd.inbox_put("approve", {"ids": ["PC-001"], "reject": True, "reason": r"R-2 must match \d+ rows"}, "2026-10-06T12:00:00+00:00")
    eng.run(exchanges=1)
    assert r"R-2 must match \d+ rows" in (repo / "docs/DECISIONS.md").read_text()
    assert not list(eng.sd.inbox.glob("*.json"))


def test_a_failing_inbox_item_is_set_aside_and_the_run_goes_on(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])

    def broken(kind: str, data: dict) -> bool:
        raise ValueError("cannot handle this")

    eng.handle_inbox_item = broken  # type: ignore[method-assign]
    eng.sd.inbox_put("approve", {"ids": ["PC-9"]}, "2026-10-06T12:00:00+00:00")
    eng.kickoff("go")
    assert eng.run() == 0 and eng.st.phase == "COMPLETE"
    failed = list((eng.sd.inbox / "failed").iterdir())
    assert any(p.suffix == ".json" for p in failed) and any(p.name.endswith(".error.txt") for p in failed)
    assert "OWNER INPUT SET ASIDE (approve)" in review_log(repo)


def test_atomic_writes_keep_mode_symlink_and_crlf(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("x\n")
    f.chmod(0o644)
    atomic_write_text(f, "y\n")
    assert f.stat().st_mode & 0o777 == 0o644
    target, link = tmp_path / "rules.md", tmp_path / "CLAUDE.md"
    target.write_text("one\n")
    link.symlink_to(target)
    atomic_write_text(link, "two\n")
    assert link.is_symlink() and target.read_text() == "two\n"
    crlf = tmp_path / "w.md"
    crlf.write_bytes(b"a\r\nb\r\n")
    atomic_write_text(crlf, "a\nb\nc\n")
    assert crlf.read_bytes() == b"a\r\nb\r\nc\r\n"
    fresh = tmp_path / "new.md"
    atomic_write_text(fresh, "z")
    mask = os.umask(0)
    os.umask(mask)
    assert fresh.stat().st_mode & 0o777 == 0o666 & ~mask


def test_a_prd_that_is_not_utf8_does_not_crash(repo: Path, clock: FakeClock) -> None:
    from agent_bridge.tui.model import ProjectWatcher

    eng = adopt(repo, clock, builder=["r0"], supervisor=[DONE])
    (repo / "docs/PRD.md").write_bytes(PRD.encode() + b"\n## Phase 3 - \xff\xfe bad bytes\n")
    snap = ProjectWatcher(repo).refresh()
    assert any("Phase 3" in title for title, _ in snap.phases)
    assert eng.current_phase() is not None


# ----------------------------------------------------------------- 5, 6. outside-repo detection


def outside(repo: str, calls: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    logged: list[str] = []
    stub = SimpleNamespace(
        cfg=SimpleNamespace(project=SimpleNamespace(repo=repo)), j=SimpleNamespace(review=lambda t, b="": logged.append(t)), st=SimpleNamespace(exchange=1)
    )
    writes = Engine._outside_repo(stub, Reply(text="", session_id="s", tool_calls=[ToolCall(n, s) for n, s in calls]))  # type: ignore[arg-type]
    return writes, logged


@pytest.mark.parametrize(
    ("calls", "flagged"),
    [
        ([("Write", "{repo}/src/app.py")], False),
        ([("bash", 'cd "{repo}" && git commit -am wip')], False),
        ([("bash", "cd src && make && cd .. && git commit -am wip")], False),
        ([("bash", "cd ~/src/other-repo && git commit -am wip")], True),
        ([("bash", "cd $HOME/src/other-repo && git commit -am wip")], True),
        ([("bash", "git -C ../other-repo commit -am wip")], True),
        ([("Write", "{home}/src/other-repo/x.py")], True),
        ([("write", "docs/PRD.md")], False),
    ],
)
def test_outside_repo_detection(calls: list[tuple[str, str]], flagged: bool) -> None:
    home = os.path.realpath(Path.home())
    repo = f"{home}/src/my project"
    writes, _ = outside(repo, [(n, s.format(repo=repo, home=home)) for n, s in calls])
    assert bool(writes) is flagged


def test_reads_outside_are_reported_not_paused() -> None:
    home = os.path.realpath(Path.home())
    writes, logged = outside(f"{home}/src/proj", [("bash", "cat ~/notes.txt")])
    assert writes == [] and any("READ OUTSIDE" in t for t in logged)


def test_a_commit_that_left_this_repo_unchanged_is_flagged(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[FakeStep(text="Committed.", tool_calls=[ToolCall("Bash", "git add -A && git commit -m x")]), "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "BUILDER WROTE BUT THE REPO DID NOT CHANGE" in review_log(repo)
    assert "HEAD did not move" in eng.backends["supervisor"].sent[0]


def test_the_workdir_rule_quotes_a_path_with_spaces() -> None:
    assert "cd '/x/My Project' &&" in prompts.workdir_rule(Path("/x/My Project"))


# ----------------------------------------------------------------- 7, 8, 10, 12. flow


def test_decide_with_questions_does_not_block_a_later_approve(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    from fixtures import QUESTIONS

    contract_files(repo)
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\n[safety]\ncaffeinate = false\n")
    assert run("approve", "--repo", str(repo)) == 0
    doc = repo / "docs/OWNER_REVIEW.md"
    doc.write_text("# Owner review\n\n## D1 Use the public sample\nYes.\n\n## Done when\n- [ ] D1 applied\n")
    fakes.scripts["planner"] = [QUESTIONS]
    assert run("decide", str(doc), "--repo", str(repo)) == 3
    fakes.scripts["planner"] = [change([("docs/OPEN.md", "The owner supplies it.", "The owner supplied it (D1).")], kickoff="Commit the doc, then apply D1.")]
    fakes.scripts["builder"] = ["applied D1"]
    fakes.scripts["supervisor"] = [DONE]
    assert run("say", "use your recommendations", "--repo", str(repo)) == 0
    assert State.load(repo / ".bridge/state.json").planning is None
    (repo / "docs/PRD.md").write_text((repo / "docs/PRD.md").read_text() + "\n<!-- owner edit -->\n")
    capsys.readouterr()
    assert run("approve", "--repo", str(repo)) == 0
    assert "re-approved" in capsys.readouterr().out


@pytest.mark.parametrize("edit", [Edit("docs/PRD.md", "- output is stable across runs\n", ""), Edit("docs/PRD.md", "- tests for R-1 pass", "- tests for R-1 pass where practical")])
def test_weakening_under_an_exit_criteria_heading_is_detected(tmp_path: Path, edit: Edit) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "bridge.toml").write_text("version = 1\n[project]\nname='x'\n")
    body = "## Phase 1 - Parser\nScope: R-1.\n\n### Exit criteria\n- tests for R-1 pass\n- output is stable across runs\n"
    (tmp_path / "docs/PRD.md").write_text("# PRD\n\n## Requirements\n- R-1: parse\n\n" + body)
    a = contract.assess(load_config(tmp_path / "bridge.toml"), [edit], planner_material=False)
    assert a.material and a.weakens


def test_a_kickoff_on_a_paused_run_ends_the_pause(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=["r1", "r2"], supervisor=[ok("next"), DONE])
    eng.kickoff("go")
    eng.sd.request_stop()
    assert eng.run() == 0 and eng.st.phase == "PAUSED"
    eng.sd.clear_stop()
    eng.kickoff("new direction")
    assert eng.st.pause is None and not eng.sd.paused.exists() and eng.st.phase == "BUILDER_TURN"
    eng.run()
    assert eng.st.phase == "COMPLETE" and eng.st.pause is None


def test_the_unknown_reset_backoff_starts_over_after_a_good_turn(repo: Path, clock: FakeClock) -> None:
    limit = SessionLimit("You've hit your limit")
    eng = make_engine(repo, clock, builder=[limit, limit, "r0", "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    assert eng.st.phase == "COMPLETE" and "limit_unknown_builder" not in eng.st.counters


# ----------------------------------------------------------------- 9. near-misses


def test_a_near_miss_completion_is_nudged_then_taken(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0"], supervisor=["PROJECT COMPLETE.\nEverything verified.", DONE])
    eng.kickoff("go")
    assert eng.run() == 0 and eng.st.phase == "COMPLETE"
    assert "was not taken as completion" in eng.backends["supervisor"].sent[1]


def test_parser_near_misses() -> None:
    from datetime import datetime, timezone
    import re

    from agent_bridge.config import DEFAULT_PHASE_PATTERN

    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert parse_supervisor("PROJECT COMPLETE: done\nVERDICT: ok\nREPLY:\nx", now=now).near_misses
    assert not parse_supervisor("PROJECT COMPLETED the parser\nVERDICT: ok\nREPLY:\nx", now=now).near_misses
    out = parse_supervisor("VERDICT: ok\nSCOPE:\n- R-4\n- Phase 3\nREPLY:\nImplement R-4.", now=now)
    assert out.scope == {"R-4", "phase 3"}
    pattern = re.compile(DEFAULT_PHASE_PATTERN)
    assert parse_builder("Done.\n\nDECISIONS NEEDED: none\n\n## Changes\n- a\n- b\n", now=now, phase_pattern=pattern).decisions_needed == []
    assert parse_builder("DECISIONS NEEDED:\n- one?\n\n## Changes\n- a\n", now=now, phase_pattern=pattern).decisions_needed == ["one?"]
    assert parse_wait_line("WAIT FOR FILE out/done.flag (written when training ends)", now=now).target == "out/done.flag"
    assert classify("Error: request failed at line 401 of handler.py") == "other"
    assert classify("API Error: 401 authentication_error") == "auth"
    assert classify("Credit balance is too low") == "billing"


# ----------------------------------------------------------------- engines


def test_billing_is_checked_on_the_first_event(repo: Path, tmp_path: Path) -> None:
    (repo / "bridge.toml").write_text("version = 1\n")
    exe = install(tmp_path / "bin", "claude")
    turn = claude_turn("ok", api_key_source="ANTHROPIC_API_KEY")
    turn["lines"].insert(1, {"sleep": 20})
    scenario(tmp_path, [turn])
    b = ClaudeCodeBackend(load_config(repo / "bridge.toml").role("builder"), repo=repo, project="demo", binary=str(exe))
    started = time.monotonic()
    with pytest.raises(Unsupported, match="would bill that key"):
        b.send("hi", timeout=60, on_event=lambda e: None, cancel=NeverCancel(), on_session=lambda s: None)
    assert time.monotonic() - started < 10


def test_child_env_strips_routing_and_cannot_be_given_a_key_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    env = child_env("subscription", {"ANTHROPIC_API_KEY": "from-env-file", "KAGGLE_API_TOKEN": "k"})
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    assert env["KAGGLE_API_TOKEN"] == "k"
    assert child_env("subscription", engine="opencode")["ANTHROPIC_BASE_URL"] == "https://proxy.invalid"
    assert child_env("api-key", {"ANTHROPIC_API_KEY": "k"})["ANTHROPIC_API_KEY"] == "k"


def test_subagent_messages_are_not_the_roles_own_and_cost_is_read() -> None:
    p = _StreamParser(lambda e: None)
    p.feed(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "mine"}]}}))
    p.feed(json.dumps({"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"model": "claude-haiku-4-5", "content": [{"type": "text", "text": "subagent says"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
    p.feed(json.dumps({"type": "result", "total_cost_usd": 0.42, "usage": {"input_tokens": 10, "output_tokens": 20}}))
    assert p.models == {"claude-opus-5-5"} and p.all_text() == "mine"
    assert [t.summary for t in p.tools] == ["ls"]
    assert p.cost_usd == 0.42 and p.usage == {"input_tokens": 10, "output_tokens": 20}


def _opencode(repo: Path, tmp_path: Path, db: OpencodeDB | None = None) -> OpencodeBackend:
    rc = RoleConfig(role="builder", engine="opencode", model="anthropic/claude-opus-5-5", variant=None, timeout=60)
    return OpencodeBackend(rc, repo=repo, project="demo", port=4999, serve_log=tmp_path / "serve.log", db=db)


def test_the_question_watcher_covers_subagents_and_only_this_turn(repo: Path, tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT)")
    conn.executemany("INSERT INTO session VALUES (?, ?, '', '')", [("ses_parent1", None), ("ses_child1", "ses_parent1"), ("ses_grand1", "ses_child1"), ("ses_other1", None)])
    conn.commit()
    conn.close()
    b = _opencode(repo, tmp_path, OpencodeDB(path))
    asked = [{"id": f"q{i}", "sessionID": s} for i, s in enumerate(("ses_parent1", "ses_grand1", "ses_other1"))]
    b._safe = lambda method, route: asked  # type: ignore[method-assign]
    assert b._questions() == []
    b._session_id = "ses_parent1"
    assert {q["sessionID"] for q in b._questions()} == {"ses_parent1", "ses_grand1"}


def test_http_errors_stay_inside_the_watcher(repo: Path, tmp_path: Path) -> None:
    b = _opencode(repo, tmp_path)

    def broken(method: str, route: str, timeout: float = 10) -> None:
        raise http.client.IncompleteRead(b"")

    b.server.api = broken  # type: ignore[method-assign]
    assert b._safe("GET", "/question") is None


def test_an_env_file_change_after_the_server_started_is_reported(repo: Path, tmp_path: Path) -> None:
    rc = RoleConfig(role="builder", engine="opencode", model="anthropic/claude-opus-5-5", variant=None, timeout=60)
    b = OpencodeBackend(rc, repo=repo, project="demo", port=4999, serve_log=tmp_path / "serve.log", env_extra={"KAGGLE_API_TOKEN": "new"})
    b.server.is_up = lambda: True  # type: ignore[method-assign]
    b.server.check_identity = lambda: "1.18.30"  # type: ignore[method-assign]
    assert any("not started by agent-bridge" in w for w in b.health_check().warnings)
    b.server.env_record.write_text(json.dumps({"env": "an older digest"}))
    assert any("env_file changed" in w for w in b.health_check().warnings)
    b.server.env_record.write_text(json.dumps({"env": b.server.env_digest()}))
    assert not any("env_file" in w for w in b.health_check().warnings)


# ----------------------------------------------------------------- designed, now built


def test_the_planner_rotates_past_its_context_limit(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, planner=[FakeStep(text="NO CHANGE: fine", context_tokens=450_000), FakeStep(text="NO CHANGE: fine")], builder=["r0"], supervisor=[DONE])
    eng._ask_planner([Block("bridge", "first")], "test")
    assert "planner" in eng.st.rotate
    eng._ask_planner([Block("bridge", "second")], "test")
    assert eng.backends["planner"].sessions_started == 2 and "PLANNER ROTATED" in review_log(repo)


def test_a_session_from_another_folder_is_replaced_at_startup(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.st.sessions["supervisor"] = {"engine": "fake", "id": "fake-supervisor-old", "closed": False}
    eng.save()
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.backends["supervisor"].session_directory = lambda sid: "/somewhere/else"  # type: ignore[method-assign]
    eng.kickoff("go")
    eng.run()
    assert "SESSION FROM ANOTHER FOLDER (supervisor)" in review_log(repo)
    assert eng.backends["supervisor"].sessions_started == 1


# ----------------------------------------------------------------- improvements


def test_the_sandbox_profile_and_command(repo: Path) -> None:
    text = sandbox.profile(repo)
    assert f'(subpath "{os.path.realpath(repo)}")' in text and '(subpath "/private/tmp")' in text and ".claude" in text
    assert sandbox.wrap(["claude", "-p"], repo)[:2] == [sandbox.SANDBOX_EXEC, "-p"]
    assert not sandbox.applies("off") and sandbox.applies("on")
    (repo / "bridge.toml").write_text("version = 1\n")
    cfg = load_config(repo / "bridge.toml")
    builder = ClaudeCodeBackend(cfg.role("builder"), repo=repo, project="demo", sandboxed=True)
    supervisor = ClaudeCodeBackend(cfg.role("supervisor"), repo=repo, project="demo", sandboxed=True)
    assert builder.capabilities().write_guard.startswith("sandboxed") and not supervisor.sandboxed


@pytest.mark.skipif(not sandbox.available(), reason="sandbox-exec is macOS-only")
def test_the_sandbox_blocks_a_write_outside_the_repo(repo: Path) -> None:
    probe = Path(f"/private/var/tmp/ab-sandbox-test-{os.getpid()}.txt")
    try:
        subprocess.run(sandbox.wrap(["/bin/sh", "-c", f"echo in > '{repo}/in.txt'; echo out > '{probe}'"], repo), capture_output=True, timeout=30)
        assert (repo / "in.txt").exists() and not probe.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_the_verify_command_runs_after_each_builder_turn(repo: Path, clock: FakeClock) -> None:
    (repo / "bridge.toml").write_text("version = 1\n[project]\nname = 'demo'\nverify = 'echo checked; exit 3'\n")
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    first = eng.backends["supervisor"].sent[0]
    assert "`echo checked; exit 3`: exit 3" in first and "checked" in first
    assert "VERIFY FAILED (exit 3)" in review_log(repo)


def test_notifications_on_pause_and_completion(repo: Path, clock: FakeClock) -> None:
    got: list[tuple[str, str]] = []
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE], notifier=lambda k, t, m: got.append((k, t)))
    eng.kickoff("go")
    eng.run()
    assert got == [("complete", "demo: PROJECT COMPLETE")]
    got.clear()
    eng = make_engine(repo, clock, builder=["r2", "r3"], supervisor=[ok("again")], toml="[budget]\nmax_exchanges = 1\n", notifier=lambda k, t, m: got.append((k, t)))
    eng.kickoff("more")
    eng.run()
    assert got and got[0][0] == "paused"


def test_a_cost_cap_pauses_the_run(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=[FakeStep(text="r0", cost_usd=2.0), FakeStep(text="r1", cost_usd=2.0)],
        supervisor=[FakeStep(text=ok("go"), cost_usd=1.5), DONE],
        toml="[budget]\nmax_cost_usd = 3\n",
    )
    eng.kickoff("go")
    eng.run()
    assert eng.st.phase == "PAUSED" and "max_cost_usd reached" in eng.st.pause["detail"]
    assert eng.st.usage["cost_usd"] == 3.5 and eng.st.usage["roles"]["builder"]["cost_usd"] == 2.0


def test_logs_rotate_and_status_reads_only_the_tail(tmp_path: Path) -> None:
    sd = StateDir(tmp_path)
    sd.ensure()
    sd.events.write_text("".join(json.dumps({"ts": "t", "kind": "k", "n": i}) + "\n" for i in range(200)))
    tail = read_events(sd.events, tail_bytes=300)
    assert tail and tail[-1]["n"] == 199 and len(tail) < 20
    assert runtime.rotate_logs(sd, limit=100) == ["events.jsonl"]
    sd.events.write_text("x" * 200)
    runtime.rotate_logs(sd, limit=100)
    assert (tmp_path / ".bridge/events.jsonl.1").exists() and (tmp_path / ".bridge/events.jsonl.2").exists()
