"""Owner to-do extraction over the ledger and open-items formats of all three migrated projects."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agent_bridge import owner
from agent_bridge.clock import IST

LEDGER = """# Tradeoffs

## 2026-10-01 — AUTONOMOUS DECISION - owner to review: raw-hash drift, lint scope

Context and choice.

## 2026-10-01 — AUTONOMOUS DECISION - owner to review: step 4c tables

Owner review: accepted (D5).

### T-001a AUTONOMOUS DECISION - owner to review: `tokenizers` and the buckets (2026-10-02)

The supervisor approved it.

## T-002 AUTONOMOUS DECISION - owner to review: FinNI scorer details (2026-10-02)

Details.

## DEC-003 Use SQLite
- Decided by: planner
- Status: AUTONOMOUS DECISION - owner to review

Why.

## DEC-004 PC-001 Split phase 3
- Decided by: planner (re-plan requested by the supervisor at exchange 41)
- Status: AWAITING OWNER

## DEC-005 Plain output
- Decided by: planner
- Status: APPROVED by the owner 2026-10-06
"""

OPEN = """# Open items

| ID | Finding |
|---|---|
| F-59 | OWNER-BLOCKED: Phase 2 exit needs ANTHROPIC_API_KEY in .env |
| F-60 | RESOLVED: was OWNER-BLOCKED until the key arrived |
| F-61 | OPEN: retrieval misses |

## O-005 Kaggle T4 Phase 0 runs (OWNER-BLOCKED via O-025)
- Status: OWNER-BLOCKED via O-025.

## OPEN-001 Host not quiet at Phase 0 start: OWNER-BLOCKED

## OPEN-002 Labels for the judge
- Status: OWNER-BLOCKED

## OPEN-003 Old blocker
- Status: RESOLVED (approved by the owner)
"""


def test_ledger_items_cover_every_format(tmp_path: Path) -> None:
    path = tmp_path / "TRADEOFFS.md"
    path.write_text(LEDGER)
    items = owner.ledger_items(path)
    decisions = [(i.ident, i.title) for i in items if i.kind == "decision"]
    assert decisions == [
        ("line 3", "2026-10-01 — raw-hash drift, lint scope"),
        ("T-001a", "`tokenizers` and the buckets (2026-10-02)"),
        ("T-002", "FinNI scorer details (2026-10-02)"),
        ("DEC-003", "Use SQLite"),
    ]
    changes = [i for i in items if i.kind == "change"]
    assert [(c.ident, c.detail) for c in changes] == [("DEC-004", "planner (re-plan requested by the supervisor at exchange 41)")]
    assert items[0].line == 3 and items[0].where(tmp_path) == "TRADEOFFS.md:3"


def test_open_items_cover_every_format(tmp_path: Path) -> None:
    path = tmp_path / "OPEN.md"
    path.write_text(OPEN)
    found = [(i.ident, i.title) for i in owner.open_items(path)]
    assert ("F-59", "Phase 2 exit needs ANTHROPIC_API_KEY in .env") in found
    assert ("O-005", "O-005 Kaggle T4 Phase 0 runs") in found
    assert ("OPEN-001", "OPEN-001 Host not quiet at Phase 0 start") in found
    assert ("OPEN-002", "OPEN-002 Labels for the judge") in found
    assert not any(ident in ("F-60", "F-61", "OPEN-003") for ident, _ in found)


def test_todo_and_template(tmp_path: Path) -> None:
    (tmp_path / "L.md").write_text(LEDGER)
    (tmp_path / "O.md").write_text(OPEN)
    found = owner.collect(tmp_path / "L.md", [tmp_path / "O.md"], [{"id": "PC-001", "title": "Split", "status": "awaiting", "affects": ["phase 3"], "diff": ".bridge/plan/changes/PC-001.diff"}, {"id": "PC-002", "status": "applied"}])
    when = datetime(2026, 10, 7, 9, 0, tzinfo=IST)
    todo = owner.render_todo("demo", tmp_path, found, when)
    assert "## Decisions to review (4)" in todo and "## OWNER-BLOCKED items (4)" in todo
    assert "- [ ] PC-001 Split (affects Phase 3; diff .bridge/plan/changes/PC-001.diff): `agent-bridge approve PC-001`" in todo
    template = owner.review_template(tmp_path, found, when)
    assert "## D1 — 2026-10-01 — raw-hash drift, lint scope (line 3, L.md:3)" in template
    assert "## D4 — Use SQLite (DEC-003, L.md:" in template
    assert "## Done when" in template and "- [ ] D4 applied" in template
