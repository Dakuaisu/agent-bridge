"""What the UI shows, read from a project's files. It never writes and never calls a model."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_bridge import contract, owner
from agent_bridge.backends.base import READ_ONLY_BY_INSTRUCTION, READ_ONLY_ENFORCED
from agent_bridge.config import CONFIG_NAME, ROLES, Config, ConfigError, load_config
from agent_bridge.engine import State
from agent_bridge.planner import load_questions
from agent_bridge.repo import Repo
from agent_bridge.report import review_entries
from agent_bridge.statedir import StateDir, lock_holder, read_json
from agent_bridge.tui.text import one_line

PLANNER_PHASES = ("PLANNING", "DRAFTING", "REPLANNING")
STATUS_LABEL = {
    "running": "RUNNING",
    "planning": "PLANNING",
    "waiting": "WAITING",
    "sleeping": "SLEEPING",
    "paused": "PAUSED",
    "stopped": "STOPPED",
    "complete": "COMPLETE",
    "review": "PLAN REVIEW",
    "answers": "NEEDS ANSWERS",
    "idle": "IDLE",
    "new": "NO PROJECT",
}


def status_of(st: State, holder: dict[str, Any] | None, configured: bool) -> str:
    if not configured:
        return "new"
    pause = st.pause or {}
    if st.phase == "INTERVIEW" or awaiting_answers(st):
        return "answers"
    if st.phase == "PLAN_REVIEW":
        return "review"
    if st.phase == "PAUSED":
        return "stopped" if pause.get("reason") == "stop" else "paused"
    if st.phase == "COMPLETE":
        return "complete"
    if st.phase == "WAITING" and holder:
        return "waiting"
    if st.phase == "SLEEPING" and holder:
        return "sleeping"
    if holder:
        return "planning" if st.phase in PLANNER_PHASES else "running"
    return "idle"


def awaiting_answers(st: State) -> bool:
    pause = st.pause or {}
    return st.phase == "INTERVIEW" or (
        st.phase == "PAUSED" and pause.get("resume") in ("PLANNING", "INTERVIEW") and (st.planning or {}).get("stage") != "approved"
    )


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def short_model(model: str) -> str:
    return model.removeprefix("anthropic/").removeprefix("claude-")


def clean_title(title: str) -> str:
    return re.sub(r"[*_`]+", "", title).strip()


# ----------------------------------------------------------------- the stream


@dataclass(frozen=True)
class Line:
    ts: str
    role: str
    kind: str
    text: str
    extra: str = ""


_GOOD = re.compile(r"PHASE COMPLETE|PHASE RECORDED AS COMPLETE|PROJECT COMPLETE|CONTRACT (?:APPROVED|RE-APPROVED)|BUILDER ROTATED")
_BAD = re.compile(r"MISMATCH|TURN CHANGED THE REPO|USED WRITE TOOLS|OUTSIDE THE REPO|DANGER|PAUSED|FAILED|REMOTE REFS")


def stream_line(e: dict[str, Any]) -> Line | None:
    kind = e.get("kind")
    ts = str(e.get("ts", ""))
    role = str(e.get("role") or "")
    if kind == "agent":
        sub, text = e.get("type"), one_line(e.get("text", ""))
        if sub == "tool":
            return Line(ts, role, "tool", str(e.get("tool") or "tool"), text)
        if sub == "text" and text:
            return Line(ts, role, "text", text)
        if sub in ("status", "error") and text:
            return Line(ts, role, "bad" if sub == "error" else "note", text)
        return None
    if kind == "turn_start":
        label = "planning" if role == "planner" else f"exchange {e.get('exchange')}"
        return Line(ts, role, "turn", f"{label}", f"{e.get('engine') or ''} · {'session ' + str(e['session'])[:8] if e.get('session') else 'new session'}")
    if kind == "turn_end":
        bits = [f"done in {_secs(e.get('seconds'))}"]
        if e.get("context_tokens"):
            bits.append(f"ctx {int(e['context_tokens']):,}")
        if e.get("models"):
            bits.append("served by " + ", ".join(short_model(m) for m in e["models"]))
        return Line(ts, role, "done", " · ".join(bits))
    if kind == "turn_error":
        return Line(ts, role, "bad", f"{e.get('error')}: {one_line(e.get('message', ''))}")
    if kind == "builder_report":
        changed = "repo changed" if e.get("changed") else "repo unchanged"
        wait = f" · asks for {e['wait']}" if e.get("wait") else ""
        return Line(ts, "builder", "report", f"report · {e.get('chars')} chars · {changed} · {e.get('decisions', 0)} decisions needed{wait}")
    if kind == "verdict":
        return Line(ts, "supervisor", "verdict", one_line(e.get("verdict", "")) or "(no verdict)", one_line(e.get("scope") or ""))
    if kind == "review":
        title = str(e.get("title", ""))
        if title.startswith("PAUSED"):
            return None
        tone = "good" if _GOOD.search(title) else "bad" if _BAD.search(title) else "alert"
        return Line(ts, "bridge", tone, title)
    if kind == "paused":
        return Line(ts, "bridge", "bad", f"PAUSED ({e.get('reason')})", one_line(e.get("detail", "")))
    if kind == "wait_start":
        return Line(ts, "bridge", "wait", f"{e.get('spec')} · asked by the {e.get('source')}", f"ends by {_hm(e.get('deadline'))} · no model calls")
    if kind == "wait_end":
        return Line(ts, "bridge", "wait", f"wait over: {e.get('outcome')}")
    if kind == "sleep_start":
        return Line(ts, "bridge", "wait", f"sleeping until {_hm(e.get('until'))}", one_line(e.get("reason", "")))
    if kind in ("kickoff", "owner_message"):
        origin = e.get("origin", "owner")
        who = "planner" if origin == "planner" else "owner"
        to = f" → {e['to']}" if e.get("to") and e.get("to") != "both" else ""
        return Line(ts, who, "message", one_line(e.get("text", "")), f"kickoff{to}" if kind == "kickoff" else f"message{to}")
    if kind == "owner_answers":
        return Line(ts, "owner", "message", one_line(e.get("text", "")), "answers")
    if kind == "plan_new":
        return Line(ts, "owner", "message", one_line(e.get("idea", "")), "new project")
    if kind in ("interview", "interview_auto"):
        return Line(ts, "planner", "ask", f"{e.get('questions')} questions for the owner")
    if kind == "plan_written":
        return Line(ts, "planner", "good", "plan written", ", ".join(e.get("files") or []))
    if kind == "plan_rejected":
        return Line(ts, "planner", "alert", "draft failed the checks", "; ".join(e.get("errors") or [])[:300])
    if kind == "replan":
        return Line(ts, "supervisor", "alert", f"REPLAN ({e.get('phase')})", one_line(e.get("problem", "")))
    if kind == "plan_change":
        return Line(ts, "planner", "change", f"{e.get('id')} {e.get('outcome')}", str(e.get("dec") or ""))
    if kind == "decide":
        return Line(ts, "owner", "message", f"decisions: {e.get('doc')}")
    if kind == "session_retired":
        return Line(ts, "bridge", "note", f"fresh {role} session", one_line(e.get("reason", "")))
    if kind in ("resumed", "recovered"):
        return Line(ts, "bridge", "note", kind, one_line(e.get("reason", "")))
    if kind == "launch":
        return Line(ts, "bridge", "launch", "agent-bridge " + " ".join(e.get("argv") or []))
    return None


def _secs(value: Any) -> str:
    s = int(float(value or 0))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _hm(value: Any) -> str:
    t = _ts(value)
    return t.strftime("%H:%M") if t else str(value or "?")


# ----------------------------------------------------------------- snapshot


@dataclass
class RoleView:
    role: str
    engine: str
    model: str
    variant: str | None
    read_only: str
    session: str | None
    limit: int
    context: int | None = None
    active: bool = False
    since: datetime | None = None
    doing: str = ""


@dataclass
class Snapshot:
    folder: Path
    repo: Path | None
    root: Path
    name: str
    configured: bool = False
    error: str | None = None
    contract_files: bool = False
    legacy: bool = False
    state: State = field(default_factory=State)
    holder: dict[str, Any] | None = None
    roles: dict[str, RoleView] = field(default_factory=dict)
    lines: list[Line] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    todo: dict[str, list[owner.TodoItem]] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    phases: list[tuple[str, str]] = field(default_factory=list)
    questions: str = ""
    plan_summary: str = ""
    last_event: datetime | None = None
    cfg: Config | None = None

    @property
    def status(self) -> str:
        return status_of(self.state, self.holder, self.configured)

    @property
    def waiting_changes(self) -> list[dict[str, Any]]:
        return [c for c in self.changes if c.get("status") == "awaiting"]

    @property
    def active(self) -> RoleView | None:
        return next((r for r in self.roles.values() if r.active), None)


class ProjectWatcher:
    """Re-reads only what changed since the last refresh; events.jsonl is tailed, never re-read whole."""

    TAIL_BYTES = 768_000
    MAX_LINES = 4000

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder).expanduser().resolve()
        top = Repo(self.folder).toplevel() if self.folder.is_dir() else None
        self.repo = Path(top).resolve() if top else None
        self.root = self.repo or self.folder
        self.sd = StateDir(self.root)
        self.cfg_path = self.root / CONFIG_NAME
        self._stamp: dict[str, Any] = {}
        self._cfg: Config | None = None
        self._cfg_error: str | None = None
        self._state = State()
        self._ino: int | None = None
        self._offset = 0
        self._partial = b""
        self._skip_first = False
        self.lines: deque[Line] = deque(maxlen=self.MAX_LINES)
        self._open_turn: dict[str, Any] | None = None
        self._context: dict[str, int] = {}
        self._doing: dict[str, str] = {}
        self._last_launch: str | None = None
        self._last_event: datetime | None = None
        self._cache: dict[str, Any] = {}
        root = str(self.root)
        self._prefixes = sorted({root + "/", root.removeprefix("/private") + "/"}, key=len, reverse=True)

    def _rel(self, text: str) -> str:
        for prefix in self._prefixes:
            text = text.replace(prefix, "")
        return text

    def _changed(self, key: str, *paths: Path) -> bool:
        stamp = []
        for p in paths:
            try:
                s = p.stat()
                stamp.append((s.st_mtime_ns, s.st_size))
            except OSError:
                stamp.append(None)
        if self._stamp.get(key) == stamp:
            return False
        self._stamp[key] = stamp
        return True

    # ------------------------------------------------------------- events

    def _read_events(self) -> None:
        path = self.sd.events
        try:
            st = path.stat()
        except OSError:
            return
        if self._ino != st.st_ino or st.st_size < self._offset:
            self._ino = st.st_ino
            self._offset = max(0, st.st_size - self.TAIL_BYTES)
            self._skip_first = self._offset > 0
            self._partial = b""
            self.lines.clear()
            self._open_turn = None
        if st.st_size <= self._offset:
            return
        with path.open("rb") as f:
            f.seek(self._offset)
            data = f.read(min(st.st_size - self._offset, 8_000_000))
        self._offset += len(data)
        chunks = (self._partial + data).split(b"\n")
        self._partial = chunks.pop()
        if self._skip_first and chunks:
            chunks = chunks[1:]
            self._skip_first = False
        for raw in chunks:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                self._ingest(e)

    def _ingest(self, e: dict[str, Any]) -> None:
        kind, role = e.get("kind"), e.get("role")
        self._last_event = _ts(e.get("ts")) or self._last_event
        if kind == "launch":
            self._last_launch = e.get("ts")
            self._open_turn = None
        elif kind == "turn_start":
            self._open_turn = e
            self._doing[str(role)] = ""
        elif kind in ("turn_end", "turn_error") and self._open_turn and role == self._open_turn.get("role"):
            self._open_turn = None
        if kind in ("turn_end", "builder_report") and e.get("context_tokens"):
            self._context[str(role or "builder")] = int(e["context_tokens"])
        if kind == "agent" and role:
            text = self._rel(one_line(e.get("text", "")))
            self._doing[str(role)] = f"{e.get('tool')} {text}".strip() if e.get("type") == "tool" else text
        line = stream_line(e)
        if line:
            self.lines.append(Line(line.ts, line.role, line.kind, self._rel(line.text), self._rel(line.extra)))

    # ------------------------------------------------------------- refresh

    def refresh(self) -> Snapshot:
        snap = Snapshot(folder=self.folder, repo=self.repo, root=self.root, name=self.root.name)
        if self._changed("config", self.cfg_path):
            self._cfg, self._cfg_error = None, None
            self._cache.pop("roles", None)
            if self.cfg_path.exists():
                try:
                    self._cfg = load_config(self.cfg_path)
                except ConfigError as e:
                    self._cfg_error = "; ".join(getattr(e, "problems", None) or [str(e)])
                except (OSError, ValueError) as e:
                    self._cfg_error = str(e)
        cfg = snap.cfg = self._cfg
        snap.configured = cfg is not None
        snap.error = self._cfg_error
        if cfg:
            snap.name = cfg.project.name
            snap.contract_files = cfg.project.prd.exists() and cfg.project.rules[0].exists()
        else:
            snap.contract_files = (self.root / "docs" / "PRD.md").exists() and (self.root / "CLAUDE.md").exists()
        snap.legacy = (self.sd.root / "session").exists() and not self.sd.state.exists()
        if self._changed("state", self.sd.state):
            try:
                self._state = State.load(self.sd.state)
            except (OSError, ValueError, TypeError):
                pass
        st = snap.state = self._state
        try:
            snap.holder = lock_holder(self.sd.lock)
        except OSError:
            snap.holder = None
        self._read_events()
        snap.lines = list(self.lines)
        snap.last_event = self._last_event
        if self._changed("review", self.sd.review_log) or self._cache.get("launch") != self._last_launch:
            self._cache["launch"] = self._last_launch
            self._cache["warnings"] = review_entries(self.sd.review_log, self._last_launch)
        snap.warnings = self._cache.get("warnings", [])
        changes_dir = self.sd.plan / "changes"
        if self._changed("changes", changes_dir):
            out = []
            for p in sorted(changes_dir.glob("PC-*.json")) if changes_dir.is_dir() else []:
                try:
                    data = read_json(p)
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    out.append(data)
            self._cache["changes"] = out
        snap.changes = self._cache.get("changes", [])
        if cfg:
            self._refresh_contract(snap, cfg, st)
        snap.roles = self._roles(cfg, st, snap.holder)
        if awaiting_answers(st):
            if self._changed("questions", self.sd.plan):
                self._cache["questions"] = load_questions(self.sd.plan) if self.sd.plan.is_dir() else ""
            snap.questions = self._cache.get("questions", "")
        if st.phase == "PLAN_REVIEW":
            plan = self.sd.plan / "plan.md"
            if self._changed("plan", plan):
                self._cache["plan"] = plan.read_text(encoding="utf-8") if plan.exists() else ""
            snap.plan_summary = self._cache.get("plan", "")
        return snap

    def _refresh_contract(self, snap: Snapshot, cfg: Config, st: State) -> None:
        p = cfg.project
        docs = (p.decisions, p.open_items, self.sd.plan / "changes")
        if self._changed("todo", *docs):
            try:
                self._cache["todo"] = owner.collect(p.decisions, [p.open_items], self._cache.get("changes", []))
            except (OSError, ValueError):
                self._cache["todo"] = {}
        snap.todo = self._cache.get("todo", {})
        files = cfg.contract_files()
        if self._changed("drift", self.sd.state, *files):
            try:
                self._cache["drift"] = contract.drifted(cfg, st.contract.get("hashes", {})) if st.contract else []
            except OSError:
                self._cache["drift"] = []
        snap.drift = self._cache.get("drift", [])
        if self._changed("phases", self.sd.state, p.prd):
            try:
                text = p.prd.read_text(encoding="utf-8")
            except OSError:
                text = ""
            self._cache["phases"] = phase_track(text, st)
        snap.phases = self._cache.get("phases", [])

    def _roles(self, cfg: Config | None, st: State, holder: dict[str, Any] | None) -> dict[str, RoleView]:
        if cfg is None:
            return {}
        active, since = None, None
        if holder:
            if self._open_turn:
                active, since = self._open_turn.get("role"), _ts(self._open_turn.get("ts"))
            elif st.phase == "BUILDER_TURN":
                active = "builder"
            elif st.phase == "SUPERVISOR_TURN":
                active = "supervisor"
            elif st.phase in PLANNER_PHASES:
                active = "planner"
        out = {}
        for role in ROLES:
            rc = cfg.role(role)
            if role == "builder":
                read_only = "writes"
            else:
                read_only = READ_ONLY_ENFORCED if rc.engine == "claude-code" else READ_ONLY_BY_INSTRUCTION
            session = st.sessions.get(role) or {}
            out[role] = RoleView(
                role=role,
                engine=rc.engine,
                model=rc.engine_model(),
                variant=rc.variant,
                read_only=read_only,
                session=None if session.get("closed") else session.get("id"),
                limit=getattr(cfg.rotation, f"{role}_max_context_tokens"),
                context=self._context.get(role),
                active=role == active,
                since=since if role == active else None,
                doing=self._doing.get(role, "") if role == active else "",
            )
        return out


def phase_track(prd_text: str, st: State) -> list[tuple[str, str]]:
    titles = [clean_title(h.title) for h in contract.headings(prd_text) if contract.phase_token(h.title)]
    done = {contract.phase_token(d["phase"]) or str(d["phase"]).lower() for d in st.phases_done}
    paused = set((st.blocked or {}).get("phases", {}))
    out, current = [], False
    for title in titles:
        token = contract.phase_token(title)
        if token in done:
            out.append((title, "done"))
        elif token in paused:
            out.append((title, "paused"))
        elif not current:
            out.append((title, "current"))
            current = True
        else:
            out.append((title, "todo"))
    if not titles:
        out = [(clean_title(str(d["phase"])), "done") for d in st.phases_done]
    return out


# ----------------------------------------------------------------- what to do next


def next_step(s: Snapshot, starting: bool = False) -> tuple[str, str]:
    """One sentence for the owner, with «k» marking keys, and a tone: info, warn, bad or good."""
    st = s.state
    pause = st.pause or {}
    if not s.configured:
        if s.error:
            return f"bridge.toml has a problem: {s.error}", "bad"
        if s.contract_files:
            return "This repo has a PRD and a CLAUDE.md. «i» sets it up for agent-bridge.", "info"
        where = "a git repo is created" if s.repo is None else "in this repo"
        return f"No agent-bridge project here. «n» plans a new one ({where}). «^A» shows all projects.", "info"
    if starting and not s.holder:
        return "Starting…", "info"
    if awaiting_answers(st):
        if st.phase == "PAUSED":
            return f"Planning paused: {one_line(pause.get('detail', ''))[:160]} «i» answers the planner.", "warn"
        return "The planner is waiting for your answers. «i» to answer.", "warn"
    if st.phase == "PLAN_REVIEW":
        return "The plan is ready. «v» to read it, «a» to approve and build.", "warn"
    waiting = s.waiting_changes
    if waiting:
        ids = ", ".join(c.get("id", "?") for c in waiting)
        return f"{ids} waits for your approval. «a» to decide.", "warn"
    if st.phase == "PAUSED":
        reason, detail = pause.get("reason", ""), one_line(pause.get("detail", ""))[:160]
        if reason == "stop":
            return "Stopped. «r» resumes where it left off.", "info"
        if "login" in f"{reason} {detail}".lower():
            return f"Paused: {detail} Run `claude login` in a terminal, then «r».", "bad"
        return f"Paused ({reason}): {detail} «r» resumes.", "bad"
    if st.phase == "COMPLETE":
        return "Project complete. «p» for the report, «d» to apply new decisions.", "good"
    if s.drift and not s.holder:
        return f"Contract changed since approval ({', '.join(s.drift)}). «a» re-approves it.", "warn"
    if st.contract is None and not s.holder:
        if st.phase in PLANNER_PHASES:
            return "Planning was interrupted. «r» continues it.", "warn"
        if s.contract_files:
            return "«a» adopts the PRD and CLAUDE.md as the approved contract; then «r» runs.", "info"
        return "No approved plan. «d» or «n» start one.", "info"
    if s.holder:
        if st.phase == "WAITING":
            w = (st.wait or {}).get("active") or {}
            spec = w.get("spec") or {}
            return f"Waiting: {spec.get('kind', '')} {spec.get('target', '')} until {_hm(w.get('deadline'))}. No model calls.", "info"
        if st.phase == "SLEEPING":
            sl = st.sleep or {}
            return f"Sleeping until {_hm(sl.get('until'))}: {one_line(sl.get('reason', ''))}", "info"
        a = s.active
        if a and a.role == "builder":
            return f"The builder is working on exchange {st.exchange}. «m» messages the agents, «s» stops.", "info"
        if a and a.role == "supervisor":
            return f"The supervisor is reviewing exchange {st.exchange}. «m» messages the agents, «s» stops.", "info"
        if a and a.role == "planner":
            return "The planner is working.", "info"
        return "Running. «s» stops at the next step boundary.", "info"
    if st.phase in PLANNER_PHASES:
        return "Planning was interrupted. «r» continues it.", "warn"
    return "Ready. «r» runs the loop, «m» queues a message for the agents.", "info"


# ----------------------------------------------------------------- the project list


@dataclass
class ProjectRow:
    repo: Path
    name: str
    status: str
    exchange: int
    holder: dict[str, Any] | None
    idle_s: float | None
    verdict: str
    missing: bool = False
    here: bool = False


def summarize(repo: Path, name: str, *, here: bool = False) -> ProjectRow:
    repo = Path(repo)
    if not repo.is_dir():
        return ProjectRow(repo, name, "new", 0, None, None, "", missing=True, here=here)
    sd = StateDir(repo)
    try:
        st = State.load(sd.state)
    except (OSError, ValueError, TypeError):
        st = State()
    try:
        holder = lock_holder(sd.lock)
    except OSError:
        holder = None
    try:
        idle = time.time() - sd.events.stat().st_mtime
    except OSError:
        idle = None
    verdict = one_line((st.last_verdict or {}).get("text", ""))
    return ProjectRow(repo, name, status_of(st, holder, (repo / CONFIG_NAME).exists()), st.exchange, holder, idle, verdict, here=here)
