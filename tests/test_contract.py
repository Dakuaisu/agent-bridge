from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_bridge import contract
from agent_bridge.config import load_config
from agent_bridge.protocol import Edit

from fixtures import DECISIONS, OPEN, PRD, RULES
from harness import BASE_TOML


@pytest.fixture
def cfg(repo: Path):
    (repo / "bridge.toml").write_text(BASE_TOML)
    (repo / "docs").mkdir()
    (repo / "docs/PRD.md").write_text(PRD)
    (repo / "CLAUDE.md").write_text(RULES)
    (repo / "docs/DECISIONS.md").write_text(DECISIONS)
    (repo / "docs/OPEN.md").write_text(OPEN)
    return load_config(repo / "bridge.toml")


def test_lint_accepts_a_good_prd() -> None:
    assert contract.lint_prd(PRD) == []


def test_lint_reports_every_missing_part() -> None:
    bad = "# PRD\n\n## Goals\n- x\n\n## Phase 1 - A\nScope: everything.\n\n## Phase 2 - B\nExit criteria:\n"
    problems = contract.lint_prd(bad)
    assert "PRD: no Non-goals heading" in problems
    assert "PRD: no Requirements heading" in problems
    assert "PRD: no Results heading" in problems
    assert "PRD: requirements are not numbered R-1, R-2, ..." in problems
    assert "PRD: 'Phase 1 - A' has no exit-criteria list" in problems
    assert "PRD: 'Phase 2 - B' has no exit-criteria list" in problems
    assert "no phase headings" in " ".join(contract.lint_prd("# PRD\n## Goals\n"))


def test_hashes_and_drift(cfg) -> None:
    approved = contract.contract_hashes(cfg)
    assert set(approved) == {"docs/PRD.md", "CLAUDE.md", "bridge.toml"}
    assert contract.drifted(cfg, approved) == []
    cfg.project.prd.write_text(PRD + "\nextra\n")
    assert contract.drifted(cfg, approved) == ["docs/PRD.md"]


def test_edits_apply_exactly_once(cfg) -> None:
    edits = [
        Edit("docs/PRD.md", "- R-2: print a summary.", "- R-2: print a summary.\n- R-3: print JSON."),
        Edit("docs/OPEN.md", "The owner supplies it.", "The owner supplies it by Friday."),
    ]
    results = contract.apply_edits_in_memory(cfg, edits, cfg.planning_docs())
    assert len(results) == 2
    diff = contract.unified_diff(cfg, results)
    assert "--- a/docs/PRD.md" in diff and "+- R-3: print JSON." in diff
    assert cfg.project.prd.read_text() == PRD  # nothing written yet
    contract.write_results(results)
    assert "R-3" in cfg.project.prd.read_text()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (Edit("docs/PRD.md", "not in the file", "x"), "occurs 0 times"),
        (Edit("docs/PRD.md", "Exit criteria:", "x"), "occurs 2 times"),
        (Edit("src/main.py", "a", "b"), "not a planning document"),
        (Edit("../outside.md", "a", "b"), "not a planning document"),
    ],
)
def test_bad_edits_are_refused(cfg, edit: Edit, message: str) -> None:
    with pytest.raises(contract.EditError, match=message):
        contract.apply_edits_in_memory(cfg, [edit], cfg.planning_docs())


def test_assess_open_items_edit_is_not_material(cfg) -> None:
    a = contract.assess(cfg, [Edit("docs/OPEN.md", "The owner supplies it.", "The owner supplies it soon.")], False)
    assert not a.material and not a.weakens


def test_assess_raises_but_never_lowers(cfg) -> None:
    a = contract.assess(cfg, [Edit("docs/OPEN.md", "x", "y")], True)
    assert a.material
    b = contract.assess(cfg, [Edit("docs/PRD.md", "- No GUI.", "- No GUI, no TUI.")], False)
    assert b.material and not b.weakens and "Non-goals" in " ".join(b.reasons)


def test_assess_changed_threshold_weakens(cfg) -> None:
    a = contract.assess(cfg, [Edit("docs/PRD.md", "- accuracy >= 0.90 on the fixture set", "- accuracy >= 0.80 on the fixture set")], False)
    assert a.material and a.weakens
    assert "phase 1" in a.affects


def test_assess_removed_exit_criterion_weakens_but_added_one_does_not(cfg) -> None:
    removed = contract.assess(cfg, [Edit("docs/PRD.md", "Exit criteria:\n- tests for R-2 pass", "Exit criteria:")], False)
    assert removed.weakens and "phase 2" in removed.affects and "R-2" in removed.affects
    added = contract.assess(cfg, [Edit("docs/PRD.md", "- tests for R-2 pass", "- tests for R-2 pass\n- docs for R-2 written")], False)
    assert added.material and not added.weakens


def test_assess_rules_edit_is_material(cfg) -> None:
    a = contract.assess(cfg, [Edit("CLAUDE.md", "Never fabricate a number.", "Never fabricate anything.")], False)
    assert a.material


def test_ledger_entries_and_status(cfg) -> None:
    path = cfg.project.decisions
    dec, line = contract.append_decision(path, title="PC-001 Split phase 1", decided_by="planner", status="AWAITING OWNER", change="PC-001 (x.diff)", body="Reason: r")
    assert dec == "DEC-010"
    lines = path.read_text().splitlines()
    assert lines[line - 1] == "## DEC-010 PC-001 Split phase 1"
    assert "- Change: PC-001 (x.diff)" in path.read_text()
    assert contract.set_decision_status(path, "DEC-010", "APPROVED by the owner 2026-10-07")
    text = path.read_text()
    assert "- Status: APPROVED by the owner 2026-10-07" in text and "AWAITING OWNER" not in text
    assert not contract.set_decision_status(path, "DEC-999", "x")


def test_new_ledger_file_gets_a_header(tmp_path: Path) -> None:
    dec, line = contract.append_decision(tmp_path / "D.md", title="t", decided_by="owner", status="s", body="b")
    assert dec == "DEC-001" and (tmp_path / "D.md").read_text().startswith("# Decisions\n")


def test_normalize_seeded_ledger() -> None:
    text = contract.normalize_seeded_ledger(DECISIONS + "\n## DEC-2 Third\n- Status: APPROVED\n- Decided by: owner\nbody\n", "PROPOSED")
    assert "## DEC-001 Use argparse\n- Decided by: planner\n- Status: PROPOSED\n" in text
    assert "## DEC-002 Plain text output" in text and "## DEC-003 Third" in text
    assert "APPROVED" not in text and "Decided by: owner" not in text
    assert contract.seeded_decisions(text) == ["DEC-001", "DEC-002", "DEC-003"]


def test_open_items(cfg) -> None:
    item, _ = contract.append_open_item(cfg.project.open_items, title="Phase 1: re-plan cap reached", status="OWNER-BLOCKED", body="details")
    assert item == "OPEN-002"
    text = cfg.project.open_items.read_text()
    assert "## OPEN-002 Phase 1: re-plan cap reached\n- Status: OWNER-BLOCKED\n- Recorded by: agent-bridge\n" in text
    contract.set_open_status(cfg.project.open_items, item, "RESOLVED (approved by the owner)")
    assert "- Status: RESOLVED (approved by the owner)" in cfg.project.open_items.read_text()
    assert "## OPEN-001 Real fixture data\n- Status: OWNER-BLOCKED" in cfg.project.open_items.read_text()


def test_claude_block_is_appended_once_and_refreshed(cfg) -> None:
    once = contract.apply_block(RULES, cfg)
    assert once.startswith(RULES) and contract.block_version(once) == 1
    assert contract.apply_block(once, cfg) == once
    stale = once.replace("agent-bridge:begin v1", "agent-bridge:begin v0").replace("Headless.", "Old text.")
    refreshed = contract.apply_block(stale + "\n## Owner notes\nkeep me\n", cfg)
    assert "Headless." in refreshed and "Old text." not in refreshed
    assert refreshed.count("agent-bridge:begin") == 1 and "keep me" in refreshed
    assert "Never push, and never add a remote." in once


def test_agents_symlink(repo: Path) -> None:
    rules = repo / "CLAUDE.md"
    rules.write_text("x")
    assert contract.ensure_agents_symlink(repo, rules) == "created"
    assert os.readlink(repo / "AGENTS.md") == "CLAUDE.md"
    assert contract.ensure_agents_symlink(repo, rules) == "ok"
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").write_text("own rules")
    assert "regular file" in contract.ensure_agents_symlink(repo, rules)
    assert (repo / "AGENTS.md").read_text() == "own rules"


def test_gitignore(repo: Path) -> None:
    assert contract.ensure_gitignore(repo)
    assert not contract.ensure_gitignore(repo)
    assert (repo / ".gitignore").read_text().count(".bridge/") == 1


def test_gitignore_adds_only_missing_entries(repo: Path) -> None:
    (repo / ".gitignore").write_text("/.bridge\n")
    assert contract.ensure_gitignore(repo, (".bridge/", ".omo/"))
    assert (repo / ".gitignore").read_text() == "/.bridge\n.omo/\n"
    assert not contract.ensure_gitignore(repo, (".bridge/", ".omo/"))
