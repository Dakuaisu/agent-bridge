from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_bridge.backends.fake import FakeStep
from agent_bridge.clock import FakeClock
from agent_bridge.engine import State
from agent_bridge.planner import ContractEngine, PlanError
from agent_bridge.statedir import StateDir

from fixtures import DECISIONS, OPEN, PRD, QUESTIONS, RULES, change, plan_text
from harness import DONE, make_engine, ok, review_log

BR = ".bridge"


def state(repo: Path) -> State:
    return State.load(repo / BR / "state.json")


def engine(repo: Path, clock: FakeClock, **kw) -> ContractEngine:
    return make_engine(repo, clock, engine_cls=ContractEngine, **kw)


def adopt(repo: Path, clock: FakeClock, *, toml: str = "", **kw) -> ContractEngine:
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/PRD.md").write_text(PRD)
    (repo / "CLAUDE.md").write_text(RULES)
    (repo / "docs/DECISIONS.md").write_text(DECISIONS)
    (repo / "docs/OPEN.md").write_text(OPEN)
    eng = engine(repo, clock, toml=toml, **kw)
    eng.approve_plan(adopt=True)
    return eng


# -- new: interview, drafting, approval


def test_new_interview_answers_draft_approve_and_kickoff(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, planner=[QUESTIONS, plan_text()], builder=["Committed the contract. Phase 1 started.", "x"], supervisor=[ok("Write the parser.", scope="R-1, Phase 1"), DONE])
    eng.plan_new("A tiny CLI that summarises files.", auto_approve=False)
    assert eng.run() == 3
    assert state(repo).phase == "INTERVIEW"
    first = eng.backends["planner"].sent[0]
    assert "ROLE FOR THIS ENTIRE SESSION: you are the PLANNER of demo" in first
    assert "[owner] (verbatim; the project idea)\nA tiny CLI that summarises files." in first
    assert "1. Which output format?" in (repo / BR / "plan/questions-1.md").read_text()

    eng.plan_answer("1: JSON. The rest as recommended.")
    assert eng.run() == 3
    assert state(repo).phase == "PLAN_REVIEW"
    assert "[owner] (verbatim; the owner's answers to your questions)\n1: JSON. The rest as recommended." in eng.backends["planner"].sent[1]
    assert (repo / "docs/PRD.md").read_text() == PRD
    rules = (repo / "CLAUDE.md").read_text()
    assert rules.startswith(RULES) and "## Running under agent-bridge" in rules
    assert (repo / "AGENTS.md").is_symlink()
    assert ".bridge/" in (repo / ".gitignore").read_text()
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "## DEC-001 Use argparse\n- Decided by: planner\n- Status: PROPOSED" in ledger
    assert "max_exchanges = 50" in (repo / "bridge.toml").read_text()
    assert "approve with `agent-bridge approve`" in (repo / BR / "plan/plan.md").read_text()

    eng.approve_plan()
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "- Status: APPROVED by the owner 2026-10-06" in ledger and "PROPOSED" not in ledger
    assert "Plan approved by the owner" in ledger
    st = state(repo)
    assert st.contract["by"] == "owner" and set(st.contract["hashes"]) == {"docs/PRD.md", "CLAUDE.md", "bridge.toml"}
    assert st.phase == "BUILDER_TURN"
    assert eng.run() == 0
    b0 = eng.backends["builder"].sent[0]
    assert "[planner]\nCommit the contract files, then start Phase 1." in b0
    s0 = eng.backends["supervisor"].sent[0]
    assert "Besides your last REPLY, the builder was told:\n[planner]\nCommit the contract files" in s0


def test_auto_approve_runs_from_idea_to_completion(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, planner=[QUESTIONS, plan_text()], builder=["phase 1 done", "x"], supervisor=[ok("go"), DONE])
    eng.plan_new("A tiny CLI.", auto_approve=True)
    assert eng.run() == 0
    assert state(repo).phase == "COMPLETE"
    assert "The owner chose --auto-approve: use your recommended answers" in eng.backends["planner"].sent[1]
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert ledger.count("- Status: AUTONOMOUS DECISION - owner to review") == 3
    assert "Plan approved under --auto-approve" in ledger


def test_plan_that_fails_the_checks_twice_pauses(repo: Path, clock: FakeClock) -> None:
    bad = plan_text(skip=("docs/OPEN.md",), prd="# PRD\n## Goals\n- x\n")
    eng = engine(repo, clock, planner=[QUESTIONS, bad, bad], builder=[], supervisor=[])
    eng.plan_new("idea", auto_approve=True)
    assert eng.run() == 3
    st = state(repo)
    assert st.pause["reason"] == "plan" and "missing file docs/OPEN.md" in st.pause["detail"]
    fix = eng.backends["planner"].sent[2]
    assert fix.startswith("[bridge] The bridge could not accept your output:") and "PRD: no Results heading" in fix
    assert not (repo / "docs/PRD.md").exists()


def test_interview_must_ask_before_drafting(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, planner=[plan_text(), QUESTIONS], builder=[], supervisor=[])
    eng.plan_new("idea", auto_approve=False)
    assert eng.run() == 3
    assert "return QUESTIONS first" in eng.backends["planner"].sent[1]
    assert state(repo).phase == "INTERVIEW"


def test_bridge_toml_is_optional_in_the_plan(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, planner=[QUESTIONS, plan_text(skip=("bridge.toml",))], builder=[], supervisor=[])
    before = (repo / "bridge.toml").read_text()
    eng.plan_new("idea", auto_approve=False)
    eng.run()
    eng.plan_answer("use your recommendations")
    assert eng.run() == 3
    assert state(repo).phase == "PLAN_REVIEW" and (repo / "bridge.toml").read_text() == before


def test_answers_after_a_failed_plan_restart_drafting(repo: Path, clock: FakeClock) -> None:
    bad = plan_text(prd="# PRD\n")
    eng = engine(repo, clock, planner=[QUESTIONS, bad, bad, plan_text()], builder=[], supervisor=[])
    eng.plan_new("idea", auto_approve=False)
    eng.run()
    eng.plan_answer("1: plain text")
    assert eng.run() == 3 and state(repo).pause["reason"] == "plan"
    assert eng.awaiting_answers()
    eng.plan_answer("use your recommendations")
    assert eng.run() == 3
    assert state(repo).phase == "PLAN_REVIEW"


def test_adopting_needs_the_prd_and_rules(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, builder=[], supervisor=[])
    with pytest.raises(PlanError, match="docs/PRD.md, CLAUDE.md missing"):
        eng.approve_plan(adopt=True)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ("[git]\npush = 'allowed'\n", "owner's to set"),
        ("[notify]\ncommand = 'touch /tmp/x'\n", "owner's to set"),
        ("[safety]\nsandbox_writable = ['~']\n", "owner's to set"),
        ("[builder]\nengine = 'opencode'\n[opencode]\nport = 4150\n", "keep [builder] engine and model"),
        ("prd = 'docs/SPEC.md'\n", "keep project.prd as docs/PRD.md"),
    ],
)
def test_planner_config_limits_under_auto_approve(repo: Path, clock: FakeClock, extra: str, error: str) -> None:
    eng = engine(repo, clock, planner=[QUESTIONS, plan_text(toml_extra=extra), plan_text()], builder=["x"], supervisor=[DONE])
    eng.plan_new("idea", auto_approve=True)
    eng.run()
    assert error in eng.backends["planner"].sent[2]


def test_run_refuses_without_a_contract(repo: Path, clock: FakeClock) -> None:
    eng = engine(repo, clock, builder=[], supervisor=[])
    eng.kickoff("go")
    assert eng.run() == 2


def test_adopting_an_existing_contract(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=["did it", "x"], supervisor=[ok("go"), DONE])
    assert "Existing contract adopted by the owner" in (repo / "docs/DECISIONS.md").read_text()
    eng.kickoff("Continue Phase 1.")
    assert eng.run() == 0


# -- re-planning


def replan(problem: str = "R-1 conflicts with the fixture data", phase: str = "Phase 1") -> str:
    return f"VERDICT: blocked\nSCOPE: none\nREPLAN ({phase}): {problem}\nREPLY:\nhold"


def test_non_material_change_is_applied(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/OPEN.md", "The owner supplies it.", "The owner supplies it; the builder uses the public sample until then.")])
    eng = adopt(repo, clock, planner=[edit], builder=["blocked on data", "used the sample"], supervisor=[replan(), ok("Use the public sample."), DONE])
    eng.kickoff("go")
    assert eng.run() == 0
    assert "public sample until then" in (repo / "docs/OPEN.md").read_text()
    s1 = eng.backends["supervisor"].sent[1]
    assert "[planner]\nPC-001 Clarify: applied (APPLIED (non-material))" in s1
    assert "Keep the builder on Phase 1." in s1
    assert "[planner]\nThe plan changed: PC-001 Clarify" in eng.backends["builder"].sent[1]
    assert "- Status: APPLIED (non-material)" in (repo / "docs/DECISIONS.md").read_text()
    record = json.loads((repo / BR / "plan/changes/PC-001.json").read_text())
    assert record["status"] == "applied" and (repo / BR / "plan/changes/PC-001.diff").read_text().startswith("--- a/docs/OPEN.md")
    assert state(repo).contract["hashes"]["docs/PRD.md"]


def test_material_change_waits_blocks_and_scope_is_enforced(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/PRD.md", "- tests for R-2 pass", "- tests for R-2 pass\n- R-2 output is JSON")], material="yes", affects="R-2, Phase 2")
    into_blocked = "VERDICT: v\nSCOPE: R-2, Phase 2\nREPLY:\nBuild the JSON printer."
    eng = adopt(repo, clock, planner=[edit], builder=["x", "y"], supervisor=[replan(phase="Phase 2"), into_blocked, into_blocked])
    eng.kickoff("go")
    assert eng.run() == 3
    assert "R-2 output is JSON" not in (repo / "docs/PRD.md").read_text()
    st = state(repo)
    assert st.blocked["changes"]["PC-001"] == ["R-2", "phase 2"]
    assert st.pause["reason"] == "scope"
    s1 = eng.backends["supervisor"].sent[1]
    assert "NOT applied; it waits for the owner" in s1
    assert "Blocked: R-2 (waiting on PC-001), Phase 2 (waiting on PC-001). Your SCOPE must avoid them." in s1
    assert "includes blocked items" in eng.backends["supervisor"].sent[2]
    assert len(eng.backends["builder"].sent) == 1
    held = list((repo / BR / "plan").glob("held-reply-*.md"))
    assert held and "Build the JSON printer." in held[0].read_text()
    opens = (repo / "docs/OPEN.md").read_text()
    assert "## OPEN-002 PC-001 Clarify waits for the owner\n- Status: OWNER-BLOCKED" in opens
    assert "agent-bridge approve PC-001" in opens


def test_builder_is_told_what_is_blocked(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/PRD.md", "- tests for R-2 pass", "- tests for R-2 pass\n- R-2 output is JSON")], material="yes", affects="R-2")
    eng = adopt(repo, clock, planner=[edit], builder=["x", "y"], supervisor=[replan(), ok("Finish the parser.", scope="R-1"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "[bridge] Blocked until the owner settles them: R-2 (waiting on PC-001), Phase 2 (waiting on PC-001). Do not work on them." in eng.backends["builder"].sent[1]


def test_material_change_is_applied_under_auto_approve(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/PRD.md", "- tests for R-2 pass", "- tests for R-2 pass\n- R-2 output is JSON")], material="yes", affects="R-2")
    eng = adopt(repo, clock, planner=[edit], builder=["x", "y"], supervisor=[replan(), ok("go"), DONE])
    eng.st.contract["auto_approve"] = True
    eng.save()
    eng.kickoff("go")
    eng.run()
    assert "R-2 output is JSON" in (repo / "docs/PRD.md").read_text()
    assert "- Status: AUTONOMOUS DECISION - owner to review\n- Change: PC-001" in (repo / "docs/DECISIONS.md").read_text()


def test_weakening_a_threshold_always_waits_for_the_owner(repo: Path, clock: FakeClock) -> None:
    edit = change([("docs/PRD.md", "- accuracy >= 0.90 on the fixture set", "- accuracy >= 0.80 on the fixture set")], material="no", affects="Phase 1")
    eng = adopt(repo, clock, planner=[edit], builder=["x", "y"], supervisor=[replan(), ok("Work on R-2 docs.", scope="none"), DONE])
    eng.st.contract["auto_approve"] = True
    eng.save()
    eng.kickoff("go")
    eng.run()
    assert "0.90" in (repo / "docs/PRD.md").read_text()
    record = json.loads((repo / BR / "plan/changes/PC-001.json").read_text())
    assert record["status"] == "awaiting" and record["weakens"] and record["planner_material"] is False
    assert "changes or removes numbers ['0.90']" in (repo / "docs/DECISIONS.md").read_text()


def test_owner_approves_and_rejects_waiting_changes(repo: Path, clock: FakeClock) -> None:
    first = change([("docs/PRD.md", "- tests for R-2 pass", "- tests for R-2 pass\n- R-2 output is JSON")], material="yes", affects="R-2", title="JSON output")
    second = change([("docs/PRD.md", "- No GUI.", "- No GUI and no web UI.")], material="yes", affects="R-1", title="No web UI")
    eng = adopt(repo, clock, planner=[first, second], builder=["x", "y", "z"], supervisor=[replan(), ok("a", scope="none"), replan("web UI scope"), ok("b", scope="none")])
    eng.kickoff("go")
    eng.run(exchanges=2)
    assert [c["id"] for c in eng.waiting_changes()] == ["PC-001", "PC-002"]
    assert eng.approve_changes(["PC-001"]) == ["PC-001: approved and applied"]
    assert eng.approve_changes(["PC-002"], reject=True, reason="out of scope") == ["PC-002: rejected"]
    assert "R-2 output is JSON" in (repo / "docs/PRD.md").read_text()
    assert "no web UI" not in (repo / "docs/PRD.md").read_text()
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "- Status: APPROVED by the owner 2026-10-06" in ledger and "- Status: REJECTED by the owner: out of scope" in ledger
    assert state(repo).blocked["changes"] == {}
    assert "RESOLVED (approved by the owner 2026-10-06)" in (repo / "docs/OPEN.md").read_text()
    st = state(repo)
    assert any("PC-001" in q for q in st.planner_queue)
    assert any("rejected PC-002" in n for n in st.supervisor_notes)
    assert eng.approve_changes(["PC-009"]) == ["PC-009: not a plan change waiting for the owner"]


def test_replan_cap_pauses_the_phase(repo: Path, clock: FakeClock) -> None:
    no = "NO CHANGE: the plan stands.\nTO SUPERVISOR:\nUse the sample."
    eng = adopt(
        repo,
        clock,
        toml="[budget]\nmax_replans_per_phase = 1\n",
        planner=[no],
        builder=["x", "y"],
        supervisor=[replan("first"), replan("second"), replan("third"), ok("Work on R-2.", scope="R-2"), DONE],
    )
    eng.kickoff("go")
    assert eng.run() == 0
    st = state(repo)
    assert st.blocked["phases"]["phase 1"].startswith("paused at the re-plan cap, OPEN-002")
    assert len(eng.backends["planner"].sent) == 1
    opens = (repo / "docs/OPEN.md").read_text()
    assert "## OPEN-002 Phase 1: re-plan cap reached\n- Status: OWNER-BLOCKED\n- Recorded by: agent-bridge" in opens
    assert "1. first\n2. second" in opens
    sent = eng.backends["supervisor"].sent
    assert "reached the re-plan cap (1); the bridge paused it" in sent[2]
    assert "Phase 1 is paused (paused at the re-plan cap, OPEN-002). Do not REPLAN it again" in sent[3]
    assert "RE-PLAN CAP REACHED for Phase 1" in review_log(repo)


def test_replan_find_mismatch_gets_one_retry(repo: Path, clock: FakeClock) -> None:
    wrong = change([("docs/OPEN.md", "text that is not there", "x")])
    right = change([("docs/OPEN.md", "The owner supplies it.", "The owner supplies it next week.")])
    eng = adopt(repo, clock, planner=[wrong, right], builder=["x", "y"], supervisor=[replan(), ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "FIND text occurs 0 times" in eng.backends["planner"].sent[1]
    assert "next week" in (repo / "docs/OPEN.md").read_text()


def test_planner_questions_during_a_replan_go_to_the_owner(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, planner=[QUESTIONS], builder=["x", "y"], supervisor=[replan(), ok("Other work.", scope="R-2"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "The planner needs the owner to re-plan Phase 1\n- Status: OWNER-BLOCKED" in (repo / "docs/OPEN.md").read_text()
    assert "cannot re-plan Phase 1 without the owner" in eng.backends["supervisor"].sent[1]


# -- owner decisions


REVIEW = "# Owner review\n\n## D1 - JSON output\nUse JSON for R-2.\n\n## Done when\n- [ ] D1 done\n"


def test_decide_goes_through_the_planner_and_starts_fresh(repo: Path, clock: FakeClock) -> None:
    decision = change(
        [("docs/PRD.md", "- R-2: print a summary.", "- R-2: print a summary as JSON.")],
        material="yes",
        affects="R-2",
        title="Owner decisions D1",
        kickoff="Commit docs/OWNER_REVIEW.md and the plan change, then do D1.",
    )
    eng = adopt(repo, clock, planner=[decision], builder=["first", "second", "D1 done"], supervisor=[ok("go"), DONE, DONE])
    eng.kickoff("go")
    eng.run()
    (repo / "docs/OWNER_REVIEW.md").write_text(REVIEW)
    eng.st.blocked["phases"]["phase 2"] = "paused at the re-plan cap, OPEN-009"
    eng.decide(repo / "docs/OWNER_REVIEW.md")
    assert eng.run() == 0
    assert "as JSON" in (repo / "docs/PRD.md").read_text()
    planner_msg = eng.backends["planner"].sent[0]
    assert "[owner] (verbatim; binding; the contents of docs/OWNER_REVIEW.md)\n# Owner review" in planner_msg
    assert "[planner]\nCommit docs/OWNER_REVIEW.md and the plan change, then do D1." in eng.backends["builder"].sent[2]
    assert "- Status: OWNER DECISION (docs/OWNER_REVIEW.md)" in (repo / "docs/DECISIONS.md").read_text()
    assert eng.backends["supervisor"].sessions_started == 2
    assert state(repo).blocked["phases"] == {}


def test_decide_refuses_a_doc_without_d_items(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=[], supervisor=[])
    (repo / "notes.md").write_text("# Notes\nsome thoughts\n")
    with pytest.raises(PlanError, match="no D-items"):
        eng.decide(repo / "notes.md")


def test_decide_while_running_is_queued(repo: Path, clock: FakeClock) -> None:
    decision = change([("docs/OPEN.md", "The owner supplies it.", "The owner supplied it.")], title="D1", kickoff="Do D1.")
    sd = StateDir(repo)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs/OWNER_REVIEW.md").write_text(REVIEW)
    eng = adopt(
        repo,
        clock,
        planner=[decision],
        builder=[FakeStep(text="r0", action=lambda: sd.inbox_put("decide", {"doc": str(repo / "docs/OWNER_REVIEW.md")}, "t")), "D1 done"],
        supervisor=[DONE],
    )
    eng.kickoff("go")
    eng.run()
    assert "[planner]\nDo D1." in eng.backends["builder"].sent[1]


# -- drift and phase boundaries


def test_builder_changing_the_prd_is_flagged_not_reverted(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=[FakeStep(text="tweaked the PRD", files={"docs/PRD.md": PRD + "\n- R-9: extra\n"}), "x"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "CONTRACT CHANGED BY THE BUILDER at exchange 0" in review_log(repo)
    assert "The builder changed docs/PRD.md during its turn" in eng.backends["supervisor"].sent[0]
    assert "R-9" in (repo / "docs/PRD.md").read_text()


def test_owner_edit_between_runs_is_not_blamed_on_the_builder(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=["x", "y"], supervisor=[ok("go"), DONE])
    (repo / "docs/PRD.md").write_text(PRD + "\n- R-3: owner added this\n")
    eng.kickoff("go")
    eng.run()
    log = review_log(repo)
    assert "CONTRACT CHANGED OUTSIDE A TURN" in log and "CHANGED BY THE BUILDER" not in log
    notes = eng.reapprove_contract()
    assert "re-approved docs/PRD.md" in notes[0]
    assert "Contract re-approved by the owner" in (repo / "docs/DECISIONS.md").read_text()


def test_phase_complete_is_recorded_in_the_ledger(repo: Path, clock: FakeClock) -> None:
    eng = adopt(repo, clock, builder=["Phase 1 complete.", "y", "z"], supervisor=["VERDICT: verified\nSCOPE: Phase 2\nPHASE COMPLETE: Phase 1\nREPLY:\nStart Phase 2.", ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "Phase 1 complete\n- Decided by: supervisor (verified at exchange 1)\n- Status: VERIFIED (phase boundary)" in ledger
    assert eng.current_phase() == "Phase 2 - Printer"


def test_supervisor_is_told_when_every_phase_is_verified(repo: Path, clock: FakeClock) -> None:
    eng = adopt(
        repo,
        clock,
        builder=["Phase 1 complete.", "Phase 2 complete.", "x"],
        supervisor=[
            "VERDICT: ok\nSCOPE: Phase 2\nPHASE COMPLETE: Phase 1\nREPLY:\nDo Phase 2.",
            "VERDICT: ok\nSCOPE: none\nPHASE COMPLETE: Phase 2\nREPLY:\nCheck everything.",
            DONE,
        ],
    )
    eng.kickoff("go")
    eng.run()
    assert "Every phase in the PRD is verified complete" not in eng.backends["supervisor"].sent[1]
    assert "Every phase in the PRD is verified complete (Phase 1 - Parser, Phase 2 - Printer)" in eng.backends["supervisor"].sent[2]
