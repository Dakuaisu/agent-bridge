"""Shared document fixtures for the contract and planner tests."""

from __future__ import annotations

from harness import BASE_TOML

PRD = """# Demo PRD

## Goals
- Ship a small CLI.

## Non-goals
- No GUI.

## Requirements
- R-1: parse the input file.
- R-2: print a summary.

## Phase 1 - Parser
Scope: R-1.
Exit criteria:
- tests for R-1 pass
- accuracy >= 0.90 on the fixture set

## Phase 2 - Printer
Scope: R-2.
Exit criteria:
- tests for R-2 pass

## Results
Real results come from the fixture set. Development runs are labelled as such.
"""

RULES = "# Demo rules\n\n- Never fabricate a number.\n"
DECISIONS = "# Decisions\n\n## DEC-7 Use argparse\nContext: a CLI.\nDecision: argparse.\nWhy: stdlib.\n\n## DEC-9 Plain text output\nWhy: simple.\n"
OPEN = "# Open items\n\n## OPEN-001 Real fixture data\n- Status: OWNER-BLOCKED\n\nThe owner supplies it.\n"


def plan_text(*, toml_extra: str = "[budget]\nmax_exchanges = 50\n", prd: str = PRD, skip: tuple[str, ...] = (), kickoff: str = "Commit the contract files, then start Phase 1.") -> str:
    files = {
        "docs/PRD.md": prd,
        "CLAUDE.md": RULES,
        "docs/DECISIONS.md": DECISIONS,
        "docs/OPEN.md": OPEN,
        "bridge.toml": BASE_TOML + toml_extra,
    }
    parts = ["PLAN:", "SUMMARY: A small CLI in two phases."]
    for path, content in files.items():
        if path in skip:
            continue
        parts += [f"=== FILE {path} ===", content.rstrip("\n"), "=== END FILE ==="]
    if kickoff:
        parts += ["KICKOFF:", kickoff]
    return "\n".join(parts) + "\n"


QUESTIONS = """QUESTIONS:
1. Which output format?
   Recommended: plain text
   Why it matters: decides R-2.
2. Python version?
   Recommended: 3.12
   Why it matters: decides the stdlib available.
"""


def change(edits: list[tuple[str, str, str]], *, material: str = "no", affects: str = "", title: str = "Clarify", kickoff: str = "") -> str:
    parts = [f"CHANGE: {title}", "REASON: the real data disagrees with the plan.", f"MATERIAL: {material}", f"AFFECTS: {affects}"]
    for path, find, replace in edits:
        parts += [f"=== EDIT {path} ===", "--- FIND ---", find, "--- REPLACE ---", replace, "=== END EDIT ==="]
    parts += ["LEDGER:", "Options: change it or not. Decision: change it.", "TO SUPERVISOR:", "Keep the builder on Phase 1."]
    if kickoff:
        parts += ["KICKOFF:", kickoff]
    return "\n".join(parts) + "\n"
