"""Project states for the terminal UI tests and previews, written with the real state and journal APIs."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from agent_bridge import contract, registry
from agent_bridge.clock import FakeClock, iso
from agent_bridge.config import load_config
from agent_bridge.engine import State
from agent_bridge.journal import Journal
from agent_bridge.statedir import BridgeLock, StateDir, atomic_write_json, atomic_write_text

PRD = """# PRD: ledger-sync

## Goals
- Reconcile a personal ledger against bank exports, line by line, with every match explained.

## Non-goals
- No bank APIs; exports only.

## Requirements
- R-1: import CSV exports from three banks.
- R-2: amounts as integer cents.
- R-3: a match explains itself.
- R-4: pair entries within a 3-day date window.

## Phase 1 - Importers
Exit criteria:
- all three formats parse; totals equal the statements

## Phase 2 - Reconciliation engine
Exit criteria:
- 95% of the fixture lines pair; every pair has a reason

## Phase 3 - Conflict review
Exit criteria:
- unpaired lines can be resolved by hand

## Phase 4 - Packaging and docs
Exit criteria:
- `pipx install .` works; README quick start runs

## Results
Real results come from the owner's exports; fixture runs are development runs.
"""

RULES = "# ledger-sync rules\n\n- Never fabricate a number.\n- Integer cents everywhere.\n"

LEDGER = """# Decisions

## DEC-001 Plan approved by the owner
- Decided by: owner
- Status: OWNER DECISION (agent-bridge approve)

## DEC-002 Amounts as integer cents, parsed with Decimal
- Decided by: builder, approved by the supervisor
- Status: AUTONOMOUS DECISION - owner to review

Floats lose cents on some exports.

## DEC-003 Phase 1 - Importers complete
- Decided by: supervisor (verified at exchange 10)
- Status: VERIFIED (phase boundary)

## DEC-004 PC-001 Date tolerance per bank
- Decided by: planner (re-plan requested by the supervisor at exchange 11)
- Status: AWAITING OWNER
"""

OPEN = """# Open items

## OPEN-001 Real exports from the owner's banks
- Status: OWNER-BLOCKED

Only the owner can download them.

## OPEN-002 Thousands separators in the HDFC export
- Status: OPEN
"""

TOML = """version = 1
[project]
name = "ledger-sync"
[planner]
engine = "claude-code"
model = "claude-fable-5-1"
variant = "max"
[supervisor]
engine = "claude-code"
model = "claude-fable-5-1"
variant = "xhigh"
[builder]
engine = "opencode"
model = "anthropic/claude-opus-5-5"
[opencode]
port = 4120
[safety]
caffeinate = false
"""


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Demo Owner"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "owner@example.invalid"], check=True)
    return path


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        atomic_write_text(repo / rel, text)


class Script:
    def __init__(self, sd: StateDir, start: datetime) -> None:
        self.clock = FakeClock(start)
        self.j = Journal(sd, self.clock, echo=False)

    def at(self, seconds: float) -> Script:
        self.clock.advance(seconds)
        return self

    def __call__(self, kind: str, **fields: object) -> None:
        if kind == "review":
            self.j.review(str(fields["title"]), str(fields.get("body", "")))
        elif kind == "launch":
            self.j.launch_marker(list(fields["argv"]))  # type: ignore[arg-type]
        else:
            self.j.event(kind, **fields)


def running(root: Path, *, hold_lock: bool = True) -> tuple[Path, BridgeLock | None]:
    repo = git_repo(root / "ledger-sync")
    _write(repo, {"docs/PRD.md": PRD, "CLAUDE.md": RULES, "docs/DECISIONS.md": LEDGER, "docs/OPEN.md": OPEN, "bridge.toml": TOML})
    cfg = load_config(repo / "bridge.toml")
    sd = StateDir(repo)
    sd.ensure()
    now = datetime.now().astimezone()
    e = Script(sd, now - timedelta(minutes=19))
    sup = "claude-code claude-fable-5-1"
    bld = "opencode anthropic/claude-opus-5-5"
    e("launch", argv=["run", "--forever"])
    e.at(2)("turn_start", role="supervisor", exchange=10, engine=sup, session="7c1e9a42-1d0b-4f7e-9a51-2b8f0c3d4e5f")
    e.at(3)("agent", role="supervisor", type="tool", tool="Read", text=f"{repo}/docs/PRD.md")
    e.at(2)("agent", role="supervisor", type="tool", tool="Bash", text="git log --oneline -5")
    e.at(2)("agent", role="supervisor", type="tool", tool="Grep", text="def parse_amount in src/")
    e.at(9)("agent", role="supervisor", type="text", text="All three bank formats parse and the totals equal the statements to the cent.")
    e.at(2)("turn_end", role="supervisor", exchange=10, seconds=20, context_tokens=148_220, models=["claude-fable-5-1"])
    e("verdict", exchange=10, verdict="Phase 1 exit criteria met: three importers, tests pass, totals reconcile.", scope="R-1, R-2, Phase 1")
    e("review", title="PHASE COMPLETE: Phase 1 - Importers (verified by the supervisor at exchange 10)")
    e("session_retired", role="builder", id="ses_8a1f2c", reason="Phase 1 - Importers complete")
    e("review", title="BUILDER ROTATED (Phase 1 - Importers complete)")
    e.at(1)("turn_start", role="builder", exchange=11, engine=bld, session=None)
    e.at(4)("agent", role="builder", type="text", text="Fresh session. Reading the handoff, then starting Phase 2: the matcher.")
    e.at(3)("agent", role="builder", type="tool", tool="read", text=f"{repo}/docs/PRD.md")
    e.at(40)("agent", role="builder", type="tool", tool="write", text=f"{repo}/src/ledger_sync/match.py")
    e.at(60)("agent", role="builder", type="tool", tool="bash", text="cd ~/src/ledger-sync && python -m pytest -q tests/test_match.py")
    e.at(5)("agent", role="builder", type="text", text="14 passed. Pairs by amount, then by the closest date inside a 5-day window.")
    e.at(30)("agent", role="builder", type="tool", tool="bash", text='git commit -m "Matcher: pair by amount and date window" -- src/ tests/')
    e.at(3)("turn_end", role="builder", exchange=11, seconds=145, context_tokens=212_400, models=["claude-opus-5-5"])
    e("builder_report", exchange=11, chars=2140, changed=True, context_tokens=212_400, decisions=1, wait=None)
    e("review", title="COMMIT ATTRIBUTION in 4f2a9c1", body="Co-Authored-By trailer")
    e.at(1)("turn_start", role="supervisor", exchange=11, engine=sup, session="7c1e9a42-1d0b-4f7e-9a51-2b8f0c3d4e5f")
    e.at(4)("agent", role="supervisor", type="tool", tool="Read", text=f"{repo}/src/ledger_sync/match.py")
    e.at(3)("agent", role="supervisor", type="tool", tool="Read", text=f"{repo}/docs/PRD.md")
    e.at(12)("agent", role="supervisor", type="text", text="The window is 5 days; R-4 says 3. Two banks post a day late, so 3 days may be too tight for them.")
    e.at(2)("turn_end", role="supervisor", exchange=11, seconds=21, context_tokens=171_800, models=["claude-fable-5-1"])
    e("verdict", exchange=11, verdict="Matcher is sound, but it uses a 5-day window where R-4 says 3; per-bank tolerance needs a plan change.", scope="R-4, Phase 2")
    e("replan", phase="Phase 2", problem="R-4's single 3-day window misses late-posting banks", count=1)
    e("plan_change", id="PC-001", outcome="awaiting", affects=["R-4", "phase 2"], dec="DEC-004")
    e("review", title="PLAN CHANGE PC-001 (awaiting the owner)")
    e.at(1)("turn_start", role="builder", exchange=12, engine=bld, session="ses_9b3e7d")
    e.at(5)("agent", role="builder", type="text", text="Setting the window to the PRD's 3 days and adding boundary tests while PC-001 waits.")
    e.at(20)("agent", role="builder", type="tool", tool="edit", text=f"{repo}/src/ledger_sync/match.py")
    e.at(25)("agent", role="builder", type="tool", tool="bash", text="python -m pytest -q")
    atomic_write_json(
        sd.plan / "changes" / "PC-001.json",
        {
            "id": "PC-001",
            "title": "Date tolerance per bank",
            "reason": "Two banks post a day late; one 3-day window misses their lines.",
            "material": True,
            "weakens": True,
            "reasons": ["docs/PRD.md: a number in an exit criterion or requirement changes"],
            "affects": ["R-4", "phase 2"],
            "diff": ".bridge/plan/changes/PC-001.diff",
            "status": "awaiting",
        },
    )
    atomic_write_text(sd.plan / "changes" / "PC-001.diff", "--- a/docs/PRD.md\n+++ b/docs/PRD.md\n@@ -12 +12 @@\n-- R-4: pair entries within a 3-day date window.\n+- R-4: pair entries within a per-bank date window (3 days; 4 for late posters).\n")
    st = State(
        phase="BUILDER_TURN",
        exchange=12,
        sessions={
            "planner": {"engine": "claude-code", "id": "1f0c2b77-aaaa-4bbb-8ccc-000000000001", "closed": False},
            "supervisor": {"engine": "claude-code", "id": "7c1e9a42-1d0b-4f7e-9a51-2b8f0c3d4e5f", "closed": False},
            "builder": {"engine": "opencode", "id": "ses_9b3e7d", "closed": False},
        },
        contract={"approved_at": iso(now - timedelta(hours=3)), "by": "owner", "hashes": contract.contract_hashes(cfg), "auto_approve": False, "drift": [], "outside_drift": []},
        phases_done=[{"phase": "Phase 1 - Importers", "exchange": 10, "at": iso(now - timedelta(minutes=17)), "dec": "DEC-003"}],
        last_verdict={"exchange": 11, "text": "Matcher is sound, but it uses a 5-day window where R-4 says 3.", "at": iso(now)},
    )
    st.save(sd.state)
    registry.remember(repo, "ledger-sync")
    lock = None
    if hold_lock:
        lock = BridgeLock(sd.lock)
        lock.acquire({"started": iso(now - timedelta(minutes=19)), "argv": ["run", "--forever"]})
    return repo, lock


QUESTIONS = """1. Where do habits live: one JSON file in the home folder, or SQLite?
   Recommended: one JSON file at ~/.habits.json
   Why it matters: decides R-2 and the backup story.
2. Does a streak survive a missed weekend for weekday-only habits?
   Recommended: yes, schedules are per habit
   Why it matters: changes the streak rule in R-4.
3. Which Python version?
   Recommended: 3.12
   Why it matters: decides the standard library available.
4. Should `habit done` accept a past date?
   Recommended: yes, with --on YYYY-MM-DD
   Why it matters: backfilling is the most common correction.
"""


def interview(root: Path) -> Path:
    repo = git_repo(root / "habit-cli")
    _write(repo, {"bridge.toml": 'version = 1\n[project]\nname = "habit-cli"\n[safety]\ncaffeinate = false\n'})
    sd = StateDir(repo)
    sd.ensure()
    now = datetime.now().astimezone()
    e = Script(sd, now - timedelta(minutes=3))
    e("launch", argv=["new", "A habit tracker CLI with streaks"])
    e("plan_new", idea="A small CLI to track daily habits and streaks, stored locally.", auto_approve=False)
    e.at(1)("turn_start", role="planner", exchange=0, engine="claude-code claude-fable-5-1", session=None)
    e.at(9)("agent", role="planner", type="text", text="Four questions decide the storage, the streak rule and the CLI surface.")
    e.at(3)("turn_end", role="planner", exchange=0, seconds=13, context_tokens=27_523, models=["claude-fable-5-1"])
    e("interview", purpose="new", questions=4)
    atomic_write_text(sd.plan / "questions-1.md", QUESTIONS)
    State(phase="INTERVIEW", planning={"stage": "interview", "idea": "habit tracker", "batches": 1, "answers": [], "attempts": 0}).save(sd.state)
    registry.remember(repo, "habit-cli")
    return repo


def plan_review(root: Path) -> Path:
    repo = git_repo(root / "wordcount")
    toml = 'version = 1\n[project]\nname = "wordcount"\n[safety]\ncaffeinate = false\n'
    _write(repo, {"docs/PRD.md": PRD.replace("ledger-sync", "wordcount"), "CLAUDE.md": RULES, "docs/DECISIONS.md": "# Decisions\n", "docs/OPEN.md": "# Open items\n", "bridge.toml": toml})
    sd = StateDir(repo)
    sd.ensure()
    now = datetime.now().astimezone()
    e = Script(sd, now - timedelta(minutes=2))
    e("launch", argv=["say", "(answers)"])
    e("owner_answers", text="use your recommendations")
    e.at(1)("turn_start", role="planner", exchange=0, engine="claude-code claude-fable-5-1", session="b4548dbb")
    e.at(30)("turn_end", role="planner", exchange=0, seconds=34, context_tokens=30_164, models=["claude-fable-5-1"])
    e("plan_written", files=["docs/PRD.md", "CLAUDE.md", "docs/DECISIONS.md", "docs/OPEN.md"], warnings=[])
    atomic_write_text(sd.plan / "plan.md", "# Plan drafted by the planner\n\nA one-function module with three pytest tests, in one phase.\n\nFiles written (uncommitted):\n- docs/PRD.md\n- CLAUDE.md\n")
    State(phase="PLAN_REVIEW", planning={"stage": "review", "files": ["docs/PRD.md"], "kickoff": "Commit the contract, then start Phase 1."}).save(sd.state)
    registry.remember(repo, "wordcount")
    return repo


def blank(root: Path, *, with_contract: bool = False) -> Path:
    repo = git_repo(root / ("old-project" if with_contract else "fresh-idea"))
    if with_contract:
        _write(repo, {"docs/PRD.md": PRD, "CLAUDE.md": RULES})
    return repo
