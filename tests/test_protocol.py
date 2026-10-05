from __future__ import annotations

import re
from datetime import datetime

import pytest

from agent_bridge.clock import IST
from agent_bridge.config import DEFAULT_PHASE_PATTERN
from agent_bridge.protocol import (
    Block,
    normalize,
    parse_builder,
    parse_planner,
    parse_supervisor,
    render,
    render_questions,
    scope_tokens,
)
from agent_bridge.waits import WaitSpec

NOW = datetime(2026, 10, 2, 22, 33, tzinfo=IST)
PHASE = re.compile(DEFAULT_PHASE_PATTERN)


def sup(text: str):
    return parse_supervisor(text, now=NOW)


# -- messages


def test_render_labels_every_block_and_never_starts_with_a_dash() -> None:
    msg = render(
        [
            Block("bridge", "Headless run: never use a question tool."),
            Block("owner", "Use Postgres.", note="(verbatim; binding)"),
            Block("bridge", ""),
            Block("supervisor", "- first: run the tests\n- then commit"),
        ]
    )
    assert msg.startswith("[bridge] Headless run")
    assert "[owner] (verbatim; binding)\nUse Postgres.\n" in msg
    assert "[supervisor]\n- first: run the tests\n- then commit\n" in msg
    assert msg.count("[bridge]") == 1


def test_unknown_origin_is_refused() -> None:
    with pytest.raises(ValueError):
        Block("developer", "pretending")


@pytest.mark.parametrize(
    ("line", "norm"),
    [
        ("**PROJECT COMPLETE**", "PROJECT COMPLETE"),
        ("## VERDICT: ok", "VERDICT: ok"),
        ("- WAIT FOR PID 5", "WAIT FOR PID 5"),
        ("> `WAIT FOR FILE /tmp/x`", "WAIT FOR FILE /tmp/x"),
        ("**REPLY:**", "REPLY:"),
        ("_SCOPE: none_", "SCOPE: none"),
        ("1. WAIT UNTIL 2026-10-03T02:41:00+05:30", "WAIT UNTIL 2026-10-03T02:41:00+05:30"),
    ],
)
def test_normalize(line: str, norm: str) -> None:
    assert normalize(line) == norm


def test_scope_tokens() -> None:
    assert scope_tokens("R-3, R-5 (Phase 2)") == {"R-3", "R-5", "phase 2"}
    assert scope_tokens("PRD 11.2 and F-59; phase 3a") == {"F-59", "phase 3a"}
    assert scope_tokens("none") == set()


# -- supervisor grammar


def test_plain_verdict_and_reply() -> None:
    out = sup("VERDICT: sound\nSCOPE: R-2, Phase 1\nREPLY:\nRun the suite, then commit.\n- one\n- two")
    assert out.verdict == "sound"
    assert out.reply == "Run the suite, then commit.\n- one\n- two"
    assert out.scope == {"R-2", "phase 1"} and not out.scope_none
    assert not out.complete and out.wait is None and not out.warnings


def test_reply_on_the_same_line_and_bold_markers() -> None:
    out = sup("**VERDICT:** ok\n**REPLY:** go to step 5")
    assert out.verdict == "ok" and out.reply == "go to step 5"


def test_missing_reply_marker() -> None:
    out = sup("I looked at the repo and everything is fine.")
    assert out.reply is None and not out.has_reply


def test_sentinel_with_a_preamble_counts() -> None:
    text = "Verified every phase.\n\nPROJECT COMPLETE\n\nWhat was built: ...\nVERDICT: done\nREPLY:\nNothing further."
    out = sup(text)
    assert out.complete
    assert "PROJECT COMPLETE" not in out.completion_text()
    assert out.reply == "Nothing further."


def test_sentinel_as_first_line_of_the_body_counts() -> None:
    out = sup("VERDICT: done\nREPLY:\n**PROJECT COMPLETE**\nsummary here")
    assert out.complete and out.reply == "summary here"


def test_sentinel_deep_in_the_body_is_ignored() -> None:
    out = sup("VERDICT: not yet\nREPLY:\nKeep going. Never write\nPROJECT COMPLETE\nyourself.")
    assert not out.complete
    assert any("ignored" in w for w in out.warnings)


def test_directives() -> None:
    text = (
        "VERDICT: waiting on the eval\n"
        "SCOPE: none\n"
        "PHASE COMPLETE: Phase 2\n"
        "- WAIT FOR PID 64410 MAX 6h\n"
        "WAIT FOR FILE /tmp/second\n"
        "ROTATE BUILDER\n"
        "REPLY:\nWhen the job exits, check the run.\nWAIT FOR PID 1 is quoted here and ignored"
    )
    out = sup(text)
    assert out.scope_none and out.scope == set()
    assert out.phase_complete == "Phase 2"
    assert out.wait == WaitSpec("pid", "64410", 21600)
    assert any("extra WAIT" in w for w in out.warnings)
    assert out.rotate_builder and not out.no_wait


def test_no_wait_and_escalate() -> None:
    out = sup("VERDICT: v\nNO WAIT\nESCALATE: which database?\nREPLY:\nx")
    assert out.no_wait and out.escalate == "which database?"


def test_replan_with_phase_and_continuation() -> None:
    out = sup(
        "VERDICT: blocked\nREPLAN (Phase 3): the exit criterion needs labels\nonly the owner can make.\n\nSCOPE: none\nREPLY:\nHold."
    )
    assert out.replan is not None
    assert out.replan.phase == "Phase 3"
    assert out.replan.problem == "the exit criterion needs labels only the owner can make."
    assert out.scope_none


def test_replan_without_phase() -> None:
    out = sup("VERDICT: v\nREPLAN: spec conflict between R-2 and R-5\nREPLY:\nwait")
    assert out.replan is not None and out.replan.phase is None
    assert "R-2" in out.replan.problem


def test_bad_wait_becomes_a_warning() -> None:
    out = sup("VERDICT: v\nWAIT UNTIL whenever\nREPLY:\nx")
    assert out.wait is None and any("ISO-8601" in w for w in out.warnings)


def test_old_bridge_output_shape_still_parses() -> None:
    raw = (
        "PROJECT COMPLETE\n\nEvery PRD section 14 phase is complete or blocked solely on owner items.\n\n"
        "VERDICT: Builder logged the timeout decision; the project is at the owner-blocked boundary.\n\n"
        "REPLY:\nVerified: the TRADEOFFS entry at line 3256 carries the decision."
    )
    out = sup(raw)
    assert out.complete and out.verdict.startswith("Builder logged")


# -- planner grammar


def test_questions() -> None:
    raw = (
        "I read the repo; it is empty.\n\nQUESTIONS:\n"
        "1. Which database?\n   Recommended: SQLite\n   Why it matters: decides the deploy story.\n"
        "2. Public or private repo?\n   Recommended: private\n   Why it matters: changes the push policy\n   and the README.\n"
    )
    out = parse_planner(raw)
    assert out.kind == "questions" and not out.errors
    assert [q.number for q in out.questions] == [1, 2]
    assert out.questions[0].recommended == "SQLite"
    assert out.questions[1].why == "changes the push policy and the README."
    assert "2. Public or private repo?" in render_questions(out.questions)


def test_plan_with_files_keeps_content_verbatim() -> None:
    raw = (
        "PLAN:\nSUMMARY: A tiny CLI.\n"
        "=== FILE docs/PRD.md ===\n# PRD\n\n## Goals\n- KICKOFF: not a marker inside a file\n=== END FILE ===\n"
        "=== FILE `CLAUDE.md` ===\nRules\n=== END FILE ===\n"
        "KICKOFF:\nCommit the contract files, then start Phase 1.\n"
    )
    out = parse_planner(raw)
    assert out.kind == "plan" and not out.errors
    assert [f.path for f in out.files] == ["docs/PRD.md", "CLAUDE.md"]
    assert out.files[0].content == "# PRD\n\n## Goals\n- KICKOFF: not a marker inside a file\n"
    assert out.summary == "A tiny CLI."
    assert out.kickoff == "Commit the contract files, then start Phase 1."


def test_change_with_edits() -> None:
    raw = (
        "CHANGE: Split phase 3\nREASON: Phase 3 cannot meet its exit criteria;\nsee eval/runs/x.json.\n"
        "MATERIAL: yes\nAFFECTS: R-4, Phase 3\n"
        "=== EDIT docs/PRD.md ===\n--- FIND ---\n## Phase 3\n- exit: all green\n--- REPLACE ---\n## Phase 3a\n- exit: all green\n=== END EDIT ===\n"
        "LEDGER:\nOptions: split, or drop R-4.\nTO SUPERVISOR:\nKeep the builder on Phase 2 items.\n"
    )
    out = parse_planner(raw)
    assert out.kind == "change" and not out.errors
    assert out.title == "Split phase 3"
    assert out.reason == "Phase 3 cannot meet its exit criteria;\nsee eval/runs/x.json."
    assert out.material is True
    assert out.affects == {"R-4", "phase 3"}
    assert out.edits[0].find == "## Phase 3\n- exit: all green"
    assert out.edits[0].replace == "## Phase 3a\n- exit: all green"
    assert out.ledger == "Options: split, or drop R-4."
    assert out.to_supervisor == "Keep the builder on Phase 2 items."


def test_no_change() -> None:
    out = parse_planner("NO CHANGE: the plan already allows this.\nTO SUPERVISOR:\nR-3 covers it.")
    assert out.kind == "no_change" and out.reason == "the plan already allows this."
    assert out.to_supervisor == "R-3 covers it."


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("Here is my thinking but no marker.", "no QUESTIONS:"),
        ("PLAN:\nSUMMARY: x\nKICKOFF:\ngo", "no === FILE"),
        ("CHANGE: t\nREASON: r\nMATERIAL: maybe\n=== EDIT a ===\n--- FIND ---\nx\n--- REPLACE ---\ny\n=== END EDIT ===", "MATERIAL"),
        ("CHANGE: t\nREASON: r\nMATERIAL: no\n", "no === EDIT"),
        ("PLAN:\n=== FILE a.md ===\nunterminated", "missing === END FILE"),
    ],
)
def test_planner_errors(raw: str, error: str) -> None:
    assert any(error in e for e in parse_planner(raw).errors)


# -- builder report


def test_builder_signals() -> None:
    text = (
        "Committed 65c442f. The resume is scheduled (pid 64410).\n"
        "Phase 1 complete; Phase 2 is next.\n\n"
        "DECISIONS NEEDED:\n1. Resume after 02:40? — about 96 items left\n- Or defer D1.3?\n\n"
        "PROCEEDING: wait for the job\n"
        "WAIT FOR PID 64410\n"
    )
    sig = parse_builder(text, now=NOW, phase_pattern=PHASE)
    assert sig.wait == WaitSpec("pid", "64410")
    assert sig.decisions_needed == ["Resume after 02:40? — about 96 items left", "Or defer D1.3?"]
    assert sig.phase_claims == ["Phase 1 complete"]


def test_builder_decisions_none_and_bad_wait() -> None:
    sig = parse_builder("DECISIONS NEEDED: none.\nWAIT UNTIL sometime", now=NOW, phase_pattern=PHASE)
    assert sig.decisions_needed == [] and sig.wait is None and "ISO-8601" in (sig.wait_error or "")
