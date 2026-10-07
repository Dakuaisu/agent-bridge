"""The loop: a persisted state machine (docs/DESIGN.md section 7).

Every transition is saved to .bridge/state.json before the action it leads to, so a crash or a
restart resumes where it stopped. Every message an agent receives is composed of labelled blocks
and recorded in loop.log exactly as delivered.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from agent_bridge import prompts
from agent_bridge.backends.base import (
    AuthFailed,
    Backend,
    BillingFailed,
    BackendError,
    Cancelled,
    Event,
    QuestionAsked,
    RateLimited,
    Reply,
    SessionLimit,
    Timeout,
    TransientError,
    Unsupported,
    select_text,
)
from agent_bridge.clock import Clock, iso
from agent_bridge.config import Config, format_duration
from agent_bridge.journal import Journal
from agent_bridge.protocol import Block, SupervisorOutput, parse_builder, parse_supervisor, render
from agent_bridge.repo import Repo, Snapshot, attribution_problems
from agent_bridge.statedir import StateDir, atomic_write_json, atomic_write_text, read_json
from agent_bridge.waits import ActiveWait, begin_wait, check_wait, parse_wait_line, pid_start_time

REPORT_CHARS = 24_000
EXIT_OK, EXIT_UNSUPPORTED, EXIT_PAUSED = 0, 2, 3
MAX_TIMEOUTS, MAX_QUESTIONS, MAX_ERRORS = 3, 3, 10
BACKOFF_START, BACKOFF_MAX = 60, 1800
LIMIT_UNKNOWN_START, LIMIT_UNKNOWN_MAX = 900, 3600
RESET_MARGIN = 120
LIMIT_SLEEP_CAP = 48 * 3600
IDLE_NUDGE_AFTER, IDLE_SLEEP_AFTER = 3, 5
IDLE_SLEEP_START, IDLE_SLEEP_MAX = 1800, 7200
SLEEP_STEP, WAIT_POLL = 5.0, 5.0

DEFAULT_DANGER = [
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b)"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\s+(?:/|~|\$HOME)"), "rm -rf on an absolute or home path"),
    (re.compile(r"(?i)\bdrop\s+(?:table|database|schema)\b"), "DROP TABLE / DATABASE"),
    (re.compile(r"(?i)co-authored-by"), "Co-Authored-By in a command"),
    (re.compile(r"\bagent-bridge\s+(?:approve|decide|engines|init|new|pin|say|stop)\b"), "an owner command"),
]
PUSH = re.compile(r"\bgit\s+push\b")
SHELL_TOOLS = {"bash", "shell"}
FILE_WRITE_TOOLS = {"write", "edit", "multiedit", "patch", "apply_patch", "notebookedit"}
OUTSIDE_PATH = re.compile(r"(?<![\w.~-])(/[^\s'\";|&()<>`]+)")
GIT_WRITES = {"commit", "add", "reset", "checkout", "switch", "push", "rm", "mv", "merge", "rebase", "stash", "clean", "restore", "tag", "cherry-pick", "revert", "am", "apply"}
WRITES_EVERY_ARG = {"rm", "rmdir", "touch", "mkdir", "truncate", "tee"}
WRITES_LAST_ARG = {"cp", "mv", "ln", "install"}
COMMAND_PREFIXES = {"sudo", "nohup", "time", "command", "exec", "env"}
SHELL_PUNCTUATION = "();<>|&\n"
# Only for a command shlex cannot parse.
SHELL_WRITE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:" + "|".join(sorted(GIT_WRITES)) + r")\b"
    r"|\b(?:rm|mv|cp|touch|mkdir|tee|truncate|ln)\s|\bsed\s+-i|>{1,2}\s*/(?!dev/)"
)


GIT_COMMIT = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")


def _expand_home(text: str, home: str) -> str:
    text = re.sub(r"\$\{HOME\}|\$HOME\b", lambda m: home, text)
    return re.sub(r"(?<![\w/.~-])~(?=/|\s|$|['\"])", lambda m: home, text)


def _mask_repo(text: str, variants: list[str]) -> str:
    """Hide the repo's own paths, which may contain spaces, so they are not split into false outside paths."""
    for v in sorted({v for v in variants if v}, key=len, reverse=True):
        text = re.sub(re.escape(v) + r"(?=/|$|[\s'\";|&)])", "AGENTBRIDGEREPO", text)
    return text


def _shell_write_targets(command: str, repo: str, home: str) -> list[str] | None:
    """Where a shell command writes, resolved from the folder each part runs in (after cd and git -C).

    Redirect targets other than /dev/*, the files of rm, touch, mkdir, tee, truncate and sed -i, the destination of
    cp, mv and ln, dd's of=, and the folder a writing git subcommand works in. None if the command cannot be parsed."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_PUNCTUATION)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    cwd: str | None = repo
    targets: list[str] = []

    def resolve(path: str, base: str | None) -> str | None:
        path = home + path[1:] if path == "~" or path.startswith("~/") else path
        path = re.sub(r"\$\{HOME\}|\$HOME\b", lambda m: home, path)
        if "$" in path or "`" in path:
            return None
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(base, path)) if base else None

    def add(path: str, base: str | None) -> None:
        if (resolved := resolve(path, base)) is not None:
            targets.append(resolved)

    def run(words: list[str]) -> None:
        nonlocal cwd
        while words and (re.match(r"^\w+=", words[0]) or words[0] in COMMAND_PREFIXES):
            words = words[1:]
        if not words:
            return
        cmd, args = os.path.basename(words[0]), words[1:]
        paths = [a for a in args if a and not a.startswith("-")]
        if cmd in ("cd", "pushd"):
            cwd = resolve(paths[0], cwd) if paths else home
        elif cmd == "git":
            where, i = cwd, 0
            while i < len(args) and args[i].startswith("-"):
                if args[i] == "-C" and i + 1 < len(args):
                    where = resolve(args[i + 1], where)
                i += 2 if args[i] in ("-C", "-c") else 1
            if i < len(args) and args[i] in GIT_WRITES and where is not None:
                targets.append(where)
        elif cmd in WRITES_EVERY_ARG:
            for path in paths:
                add(path, cwd)
        elif cmd in WRITES_LAST_ARG and len(paths) >= 2:
            add(paths[-1], cwd)
        elif cmd == "sed" and any(a.startswith(("-i", "--in-place")) for a in args):
            files, skip, script_given = [], False, False
            for a in args:
                if skip:
                    skip = False
                elif a in ("-e", "-f", "--expression", "--file"):
                    skip = script_given = True
                elif a and not a.startswith("-"):
                    files.append(a)
            for path in files if script_given else files[1:]:
                add(path, cwd)
        elif cmd == "dd":
            for a in args:
                if a.startswith("of="):
                    add(a[3:], cwd)

    words: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok or set(tok) - set(SHELL_PUNCTUATION):
            words.append(tok)
        elif ">" in tok:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            fd_copy = tok.endswith("&") and (target.isdigit() or target == "-")
            if target and not fd_copy and not target.startswith("/dev/"):
                add(target, cwd)
            i += 1
        elif "<" in tok:
            i += 1
        else:
            run(words)
            words = []
        i += 1
    run(words)
    return targets


def _shell_dirs(command: str, repo: str, home: str) -> list[str]:
    """The folders a shell command works in: each cd target and git -C path, followed from the repo."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    cwd, out, i = repo, [], 0

    def resolve(target: str) -> str:
        target = home + target[1:] if target == "~" or target.startswith("~/") else target
        target = re.sub(r"\$\{HOME\}|\$HOME\b", lambda m: home, target)
        return os.path.normpath(os.path.join(cwd, target))

    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd" and i + 1 < len(tokens):
            target = tokens[i + 1]
            if target == "-" or "`" in target or re.search(r"\$(?!\{?HOME\b)", target):
                break
            cwd = resolve(target)
            out.append(cwd)
            i += 2
            continue
        if tok == "git" and i + 2 < len(tokens) and tokens[i + 1] == "-C":
            if "$" not in tokens[i + 2] or re.search(r"\$\{?HOME\b", tokens[i + 2]):
                out.append(resolve(tokens[i + 2]))
            i += 3
            continue
        i += 1
    return out


class FatalError(Exception):
    """Unsupported version, flags or billing: stop with the fix, never retry."""


@dataclass
class State:
    schema: int = 1
    phase: str = "IDLE"
    exchange: int = 0
    pending: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    wait: dict[str, Any] | None = None
    sleep: dict[str, Any] | None = None
    pause: dict[str, Any] | None = None
    replan: dict[str, Any] | None = None
    sessions: dict[str, Any] = field(default_factory=dict)
    rotate: dict[str, str] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    feed: list[dict[str, str]] = field(default_factory=list)
    owner_queue: list[dict[str, Any]] = field(default_factory=list)
    supervisor_head: str | None = None
    contract: dict[str, Any] | None = None
    blocked: dict[str, Any] = field(default_factory=lambda: {"changes": {}, "phases": {}})
    replans: dict[str, int] = field(default_factory=dict)
    phases_done: list[dict[str, Any]] = field(default_factory=list)
    last_verdict: dict[str, Any] | None = None
    complete: dict[str, Any] | None = None
    planning: dict[str, Any] | None = None
    handoff: dict[str, str] = field(default_factory=dict)
    planner_queue: list[str] = field(default_factory=list)
    supervisor_notes: list[str] = field(default_factory=list)
    replan_history: dict[str, list[str]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        atomic_write_json(path, asdict(self))


def new_pending(**kw: Any) -> dict[str, Any]:
    return {"supervisor": None, "supervisor_note": "", "planner": [], "notes": [], "attempt": 0, "in_flight": False, **kw}


def set_note(pending: dict[str, Any], kind: str, text: str) -> None:
    """One note per kind: a second timeout replaces the first resume note instead of stacking."""
    notes = [n for n in pending.get("notes", []) if not (isinstance(n, dict) and n.get("kind") == kind)]
    pending["notes"] = [*notes, {"kind": kind, "text": text}]


def note_text(note: Any) -> str:
    return note["text"] if isinstance(note, dict) else str(note)


class Engine:
    def __init__(
        self,
        cfg: Config,
        sd: StateDir,
        journal: Journal,
        backends: dict[str, Backend],
        *,
        clock: Clock,
        repo: Repo | None = None,
        confirm: Callable[[str, str], str | None] | None = None,
        on_complete: Callable[[Engine], None] | None = None,
        pid_start: Callable[[int], str | None] = pid_start_time,
        notifier: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.sd = sd
        self.j = journal
        self.backends = backends
        self.clock = clock
        self.repo = repo or Repo(cfg.project.repo)
        self.confirm = confirm
        self.on_complete = on_complete
        self.pid_start = pid_start
        self.notifier = notifier
        self._run_cost = 0.0
        for backend in backends.values():
            backend.on_process = self._track_agent
        self.st = State.load(sd.state)
        self.enforcement = {role: b.capabilities().read_only for role, b in backends.items()}
        self._replies_this_run = 0
        self._exchange_limit: int | None = None
        self._run_started = self.clock.now()
        self._last_supervisor_context: int | None = None
        self._restore_sessions()

    # ------------------------------------------------------------------ state

    def save(self) -> None:
        self.st.save(self.sd.state)

    def now(self) -> datetime:
        return self.clock.now()

    def _restore_sessions(self) -> None:
        for role, backend in self.backends.items():
            info = self.st.sessions.get(role) or {}
            # A role moved to another engine in bridge.toml cannot resume the old engine's session.
            if info.get("id") and not info.get("closed") and info.get("engine") in (None, backend.engine):
                backend.resume(info["id"])
            else:
                backend.start_session(self._title(role))

    def _title(self, role: str) -> str:
        return f"{self.cfg.project.name} {role} {self.now():%Y-%m-%d %H:%M}"

    def _record_session(self, role: str, session_id: str) -> None:
        info = self.st.sessions.get(role) or {}
        if info.get("id") == session_id and not info.get("closed"):
            return
        backend = self.backends[role]
        self.st.sessions[role] = {"engine": backend.engine, "id": session_id, "started": iso(self.now()), "closed": False}
        registry = read_json(self.sd.sessions, default=[]) or []
        registry.append(
            {
                "role": role,
                "engine": backend.engine,
                "model": backend.cfg.engine_model(),
                "id": session_id,
                "title": backend._title,
                "directory": str(backend.workdir),
                "created": iso(self.now()),
                "retired": None,
            }
        )
        atomic_write_json(self.sd.sessions, registry)
        self.save()
        self.j.event("session", role=role, engine=backend.engine, id=session_id)

    def retire_session(self, role: str, reason: str) -> str | None:
        info = self.st.sessions.get(role) or {}
        old = info.get("id")
        if old:
            registry = read_json(self.sd.sessions, default=[]) or []
            for entry in registry:
                if entry.get("id") == old and entry.get("retired") is None:
                    entry["retired"] = iso(self.now())
                    entry["retired_reason"] = reason
            atomic_write_json(self.sd.sessions, registry)
        self.st.sessions[role] = {**info, "closed": True}
        self.backends[role].start_session(self._title(role))
        self.j.event("session_retired", role=role, id=old, reason=reason)
        return old

    # ------------------------------------------------------------------ owner input

    def _new_work(self) -> None:
        if self.st.phase == "COMPLETE" or (self.st.sessions.get("supervisor") or {}).get("closed"):
            self.st.rotate["supervisor"] = "new work after PROJECT COMPLETE"
        self.st.complete = None

    def kickoff(self, text: str, *, origin: str = "owner") -> None:
        """A first message for the builder; the supervisor sees it in its next turn."""
        if self.st.phase == "PAUSED":
            self._resume_from_pause()
        self._new_work()
        pending = self.st.pending or new_pending()
        if origin == "owner":
            self._queue_owner(text, to_builder=True, to_supervisor=True)
        else:
            pending["planner"] = [*pending["planner"], text]
        if self.st.phase == "SUPERVISOR_TURN":
            self.j.event("review_skipped", reason="a kickoff replaced the pending review", exchange=self.st.exchange)
            self.st.review = None
        if self.st.phase == "WAITING" and self.st.wait:
            pending = self.st.wait.get("pending") or pending
            self.st.wait = None
        self.st.pending = pending
        self.st.phase = "BUILDER_TURN"
        self.save()
        self.j.event("kickoff", origin=origin, text=text)

    def _queue_owner(self, text: str, *, to_builder: bool, to_supervisor: bool) -> None:
        self.st.owner_queue.append(
            {"text": text, "to_builder": to_builder, "to_supervisor": to_supervisor, "delivered": False, "shown": False, "at": iso(self.now())}
        )

    def owner_message(self, text: str, to: str = "both") -> bool:
        """An owner `say`. Returns True if it is bound for the builder (so it ends a wait)."""
        to_builder = to in ("both", "builder")
        to_supervisor = to in ("both", "supervisor")
        self._queue_owner(text, to_builder=to_builder, to_supervisor=to_supervisor)
        if to_builder and self.st.phase == "WAITING" and self.st.wait:
            self.st.wait["interrupted"] = True
        if to_builder and self.st.phase in ("IDLE", "COMPLETE"):
            self._new_work()
            self.st.pending = self.st.pending or new_pending()
            self.st.phase = "BUILDER_TURN"
        self.save()
        self.j.event("owner_message", to=to, text=text)
        return to_builder

    def take_inbox(self) -> bool:
        """Deliver queued owner input into the state. True if something is bound for the builder."""
        for_builder = False
        for item in self.sd.inbox_peek():
            try:
                if item.kind == "say":
                    for_builder |= self.owner_message(str(item.data.get("text", "")), str(item.data.get("to", "both")))
                else:
                    for_builder |= self.handle_inbox_item(item.kind, item.data)
            except Exception as e:  # noqa: BLE001 - one bad item must not stop every later run
                where = self.sd.inbox_set_aside(item, f"{type(e).__name__}: {e}")
                self.j.review(f"OWNER INPUT SET ASIDE ({item.kind})", f"{type(e).__name__}: {e}\nmoved to {where}; nothing else was changed")
                self.j.console(f"owner input ({item.kind}) failed and was set aside: {e}")
                continue
            self.sd.inbox_ack(item)
        return for_builder

    def handle_inbox_item(self, kind: str, data: dict[str, Any]) -> bool:
        """Approvals and decisions docs; handled by the planner layer."""
        self.j.event("inbox_ignored", item=kind)
        return False

    # ------------------------------------------------------------------ the loop

    def run(self, *, exchanges: int | None = None) -> int:
        self._exchange_limit = exchanges
        self._replies_this_run = 0
        self._run_started = self.now()
        self._run_cost = 0.0
        if self.st.phase == "PAUSED":
            self._resume_from_pause()
        self._end_leftover_agent()
        self._verify_sessions()
        self._recover_in_flight()
        self.j.event("run_start", phase=self.st.phase, exchange=self.st.exchange, limit=exchanges)
        try:
            while True:
                if self.sd.stop_requested():
                    return self._stop()
                self.take_inbox()
                cap = self._budget_cap()
                if cap:
                    return self.pause("budget", cap)
                code = self.step()
                if code is not None:
                    return code
        except FatalError as e:
            self.j.console(f"STOPPED: {e}")
            self.j.review("UNSUPPORTED: run stopped", str(e))
            return EXIT_UNSUPPORTED

    def step(self) -> int | None:
        phase = self.st.phase
        if phase == "BUILDER_TURN":
            return self._builder_turn()
        if phase == "SUPERVISOR_TURN":
            if self._exchange_limit is not None and self._replies_this_run >= self._exchange_limit:
                self.j.console(f"Done: {self._replies_this_run} exchange(s). Next: the supervisor reviews the last report.")
                return EXIT_OK
            return self._supervisor_turn()
        if phase == "WAITING":
            return self._waiting()
        if phase == "SLEEPING":
            return self._sleeping()
        if phase == "REPLANNING":
            return self.replanning()
        if phase == "PLANNING":
            return self.planning_step()
        if phase == "COMPLETE":
            return EXIT_OK
        if phase == "PAUSED":
            return EXIT_PAUSED
        if phase in ("INTERVIEW", "DRAFTING", "PLAN_REVIEW"):
            self.j.console(f"The plan is not approved yet ({phase}); see `agent-bridge status`.")
            return EXIT_PAUSED
        self.j.console("Nothing to do: no kickoff, no pending message and no report to review.")
        return EXIT_UNSUPPORTED

    def replanning(self) -> int | None:
        return self.pause("error", "REPLANNING needs the planner layer")

    def planning_step(self) -> int | None:
        return self.pause("error", "PLANNING needs the planner layer")

    def _resume_from_pause(self) -> None:
        pause = self.st.pause or {}
        self.st.phase = pause.get("resume") or "IDLE"
        self.st.pause = None
        self.sd.clear_stop()
        self.sd.paused.unlink(missing_ok=True)
        self.save()
        self.j.event("resumed", reason=pause.get("reason"), phase=self.st.phase)

    def _track_agent(self, proc: Any) -> None:
        """Remember the running agent process: a bridge killed with SIGKILL cannot end it, its next start can."""
        if proc is None:
            self.sd.agent_pid.unlink(missing_ok=True)
            return
        try:
            atomic_write_json(self.sd.agent_pid, {"pid": proc.pid, "start": self.pid_start(proc.pid), "at": iso(self.now())})
        except OSError:
            pass

    def _end_leftover_agent(self) -> None:
        path = self.sd.agent_pid
        try:
            info = read_json(path, default=None)
        except (OSError, ValueError):
            info = None
        pid, start = (info.get("pid"), info.get("start")) if isinstance(info, dict) else (None, None)
        if isinstance(pid, int) and start and self.pid_start(pid) == start:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except OSError:
                    break
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self.pid_start(pid) == start:
                    time.sleep(0.1)
                if self.pid_start(pid) != start:
                    break
            self.j.review(
                "LEFTOVER AGENT PROCESS ENDED",
                f"pid {pid} from a previous run was still working on its turn (that bridge was killed); it was ended before the turn is resent",
            )
            self.j.console(f"ended a leftover agent process (pid {pid}) from a previous run")
        path.unlink(missing_ok=True)

    def _verify_sessions(self) -> None:
        """A stored session must belong to this repo (or the role's own folder); otherwise start a fresh one."""
        repo = os.path.realpath(self.cfg.project.repo)
        for role, backend in self.backends.items():
            info = self.st.sessions.get(role) or {}
            sid = info.get("id")
            if not sid or info.get("closed") or info.get("engine") not in (None, backend.engine):
                continue
            try:
                directory = backend.session_directory(sid)
            except Exception:  # noqa: BLE001 - a check, never a reason to stop
                continue
            if directory is None:
                continue
            if os.path.realpath(directory) not in {repo, os.path.realpath(backend.workdir)}:
                self.j.review(f"SESSION FROM ANOTHER FOLDER ({role})", f"session {sid} belongs to {directory}, not {repo}; a fresh session starts instead")
                self.retire_session(role, f"its stored session belonged to {directory}")

    def _recover_in_flight(self) -> None:
        st = self.st
        if st.phase == "BUILDER_TURN" and st.pending and st.pending.get("in_flight"):
            self.backends["builder"].abort()
            set_note(st.pending, "restart", prompts.RESTART_NOTE)
            st.pending["in_flight"] = False
            self.save()
            self.j.event("recovered", role="builder", reason="the bridge stopped during a builder turn")
        if st.phase == "SUPERVISOR_TURN" and st.review and st.review.get("in_flight"):
            self.backends["supervisor"].abort()
            st.review["in_flight"] = False
            self.save()
            self.j.event("recovered", role="supervisor", reason="the bridge stopped during a supervisor turn")

    def _budget_cap(self) -> str | None:
        b = self.cfg.budget
        if b.max_exchanges is not None and self._replies_this_run >= b.max_exchanges and self.st.phase == "SUPERVISOR_TURN":
            return f"max_exchanges reached: {self._replies_this_run} exchanges in this run"
        if b.max_wall_time is not None and (self.now() - self._run_started).total_seconds() >= b.max_wall_time:
            return f"max_wall_time reached: {format_duration(b.max_wall_time)} since this run started"
        if b.max_cost_usd is not None and self._run_cost >= b.max_cost_usd:
            return f"max_cost_usd reached: ${self._run_cost:.2f} of usage in this run, at API prices (cap ${b.max_cost_usd:.2f})"
        unchanged = self.st.counters.get("unchanged", 0)
        if b.max_unchanged_exchanges is not None and unchanged >= b.max_unchanged_exchanges and self.st.phase != "WAITING":
            return f"max_unchanged_exchanges reached: {unchanged} exchanges in a row changed nothing in the repo"
        return None

    # ------------------------------------------------------------------ builder

    def builder_message(self) -> tuple[str, list[Block]]:
        p = self.st.pending or new_pending()
        blocks = [Block("bridge", prompts.HEADLESS), Block("bridge", prompts.workdir_rule(self.cfg.project.repo))]
        blocked = self.blocked_tokens()
        if blocked:
            blocks.append(Block("bridge", f"Blocked until the owner settles them: {', '.join(blocked)}. Do not work on them."))
        for item in self.st.owner_queue:
            if item["to_builder"] and not item["delivered"]:
                blocks.append(Block("owner", item["text"], note="(verbatim; binding)"))
        for text in [*self.st.planner_queue, *p.get("planner", [])]:
            blocks.append(Block("planner", text))
        for note in p.get("notes", []):
            blocks.append(Block("bridge", note_text(note)))
        if p.get("supervisor"):
            blocks.append(Block("supervisor", p["supervisor"], note=p.get("supervisor_note", "")))
        return render(blocks), blocks

    def blocked_tokens(self) -> list[str]:
        """Requirements and phases the builder must not touch; filled by the planner layer."""
        return []

    def _builder_turn(self) -> int | None:
        p = self.st.pending or new_pending()
        rotate_reason = self.st.rotate.pop("builder", None)
        if rotate_reason:
            old = self.retire_session("builder", rotate_reason)
            set_note(p, "handoff", prompts.handoff_note(self.cfg, old, rotate_reason))
            self.j.review(f"BUILDER ROTATED ({rotate_reason})", f"retired session {old}")
        self.st.pending = p
        message, blocks = self.builder_message()
        p["attempt"] = p.get("attempt", 0) + 1
        p["in_flight"] = True
        self.save()
        n = self.st.exchange
        backend = self.backends["builder"]
        self.j.transcript(f"EXCHANGE {n} | to builder ({backend.describe()})", message)
        self.j.event("message", recipient="builder", exchange=n, blocks=[{"origin": b.origin, "note": b.note} for b in blocks])
        before = self.repo.snapshot()
        self.before_builder_turn()
        started = self.now()
        try:
            reply = self._send("builder", message)
        except BackendError as e:
            p["in_flight"] = False
            self.save()
            return self._turn_failed("builder", e)
        after = self.repo.snapshot()
        self._delivered(blocks)
        return self._after_builder(reply, before, after, started)

    def _delivered(self, blocks: list[Block]) -> None:
        """Mark owner messages delivered, and keep what else the builder was told for the supervisor."""
        for item in self.st.owner_queue:
            if item["to_builder"] and not item["delivered"]:
                item["delivered"] = True
        self.st.planner_queue = []
        for b in blocks:
            if b.origin == "planner":
                self.st.feed.append({"origin": "planner", "text": b.text})
            elif b.origin == "bridge" and b.text != prompts.HEADLESS and not b.text.startswith(("Blocked until", "The repository is ")):
                self.st.feed.append({"origin": "bridge", "text": b.text})
        self._prune_owner_queue()

    def _prune_owner_queue(self) -> None:
        self.st.owner_queue = [
            i for i in self.st.owner_queue if (i["to_builder"] and not i["delivered"]) or (i["to_supervisor"] and not i["shown"])
        ]

    def _after_builder(self, reply: Reply, before: Snapshot, after: Snapshot, started: datetime) -> int | None:
        cfg, st = self.cfg, self.st
        atomic_write_text(self.sd.builder_last, reply.text)
        report_path = self.sd.turns / f"{st.exchange:04d}-builder-report.md"
        atomic_write_text(report_path, reply.text)
        self.j.transcript(f"EXCHANGE {st.exchange} | builder report", reply.text)
        self._check_models("builder", reply)
        self._audit_builder(reply, before, after)
        outside = self._outside_repo(reply)
        notes = self._wrote_elsewhere(reply, before, after) + self.after_builder_audit(before, after)
        verify = self._verify()
        signals = parse_builder(reply.text, now=self.now(), phase_pattern=cfg.rotation.phase_complete_pattern)
        changed = before.fingerprint() != after.fingerprint()
        st.counters["unchanged"] = 0 if changed else st.counters.get("unchanged", 0) + 1
        if reply.context_tokens and reply.context_tokens >= cfg.rotation.builder_max_context_tokens:
            st.rotate["builder"] = f"context reached {reply.context_tokens:,} tokens"
        st.review = {
            "report": str(report_path),
            "exchange": st.exchange,
            "duration_s": (self.now() - started).total_seconds(),
            "changed": changed,
            "wait": signals.wait.describe() if signals.wait else None,
            "wait_error": signals.wait_error,
            "decisions": signals.decisions_needed,
            "phase_claims": signals.phase_claims,
            "context_tokens": reply.context_tokens,
            "nudges": [],
            "notes": notes,
            "verify": verify,
        }
        st.pending = None
        st.phase = "SUPERVISOR_TURN"
        for key in ("timeouts_builder", "questions_builder", "errors_builder", "limit_sleep_s", "limit_unknown_builder"):
            st.counters.pop(key, None)
        self.save()
        self.j.event(
            "builder_report",
            exchange=st.exchange,
            chars=len(reply.text),
            changed=changed,
            context_tokens=reply.context_tokens,
            decisions=len(signals.decisions_needed),
            wait=st.review["wait"],
        )
        if outside:
            return self.pause(
                "outside the repo",
                f"the builder changed files outside {cfg.project.repo} at exchange {st.exchange}: {'; '.join(outside)}. "
                "Check those places (another repository may have changed), then `agent-bridge run` to continue.",
                resume="SUPERVISOR_TURN",
            )
        unchanged = st.counters["unchanged"]
        if unchanged >= IDLE_SLEEP_AFTER and not signals.wait:
            seconds = min(IDLE_SLEEP_START * 2 ** (unchanged - IDLE_SLEEP_AFTER), IDLE_SLEEP_MAX)
            self.j.review(f"IDLE BACKSTOP at exchange {st.exchange}", f"{unchanged} exchanges in a row changed nothing; sleeping {format_duration(seconds)} before the supervisor's turn")
            return self._sleep_until(self.now() + timedelta(seconds=seconds), f"idle backstop: {unchanged} exchanges without a repo change", resume="SUPERVISOR_TURN")
        return None

    def _wrote_elsewhere(self, reply: Reply, before: Snapshot, after: Snapshot) -> list[str]:
        """The L22 signature: the builder wrote or committed, yet this repo did not change."""
        notes = []
        commits = [
            c for c in reply.tool_calls if c.name.lower() in SHELL_TOOLS and GIT_COMMIT.search(c.summary) and "--dry-run" not in c.summary
        ]
        if commits and before.head == after.head:
            notes.append(
                "The builder ran `git commit`, but this repo's HEAD did not move: the commit failed, or it went to another "
                "repository. Check before accepting the report."
            )
        if any(c.name.lower() in FILE_WRITE_TOOLS for c in reply.tool_calls) and before.fingerprint() == after.fingerprint():
            notes.append(
                "The builder used file-writing tools, but nothing in this repo changed: the writes went to ignored files or to "
                "another folder. Check before accepting the report."
            )
        for note in notes:
            self.j.review(f"BUILDER WROTE BUT THE REPO DID NOT CHANGE at exchange {self.st.exchange}", note)
        return notes

    def _verify(self) -> dict[str, Any] | None:
        """The project's own check (bridge.toml project.verify), run by the bridge, so the supervisor has evidence."""
        command = self.cfg.project.verify
        if not command:
            return None
        from agent_bridge.backends.claude_code import child_env
        from agent_bridge.backends.proc import kill_group

        started = time.monotonic()
        code: int | None
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=self.cfg.project.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                start_new_session=True,
                env=child_env(self.cfg.billing_mode, None),
            )
        except OSError as e:
            output, code = f"could not start: {e}", -1
        else:
            try:
                output, _ = proc.communicate(timeout=self.cfg.project.verify_timeout)
                code = proc.returncode
            except BaseException:
                kill_group(proc, grace=5.0)
                output = proc.communicate()[0] if proc.stdout else ""
                code = None
                if not isinstance(sys.exc_info()[1], subprocess.TimeoutExpired):
                    raise
        seconds = round(time.monotonic() - started, 1)
        tail = "\n".join((output or "").splitlines()[-40:])[-4000:]
        result = {"command": command, "exit": code, "seconds": seconds, "tail": tail}
        self.j.event("verify", exchange=self.st.exchange, exit=code, seconds=seconds)
        if code != 0:
            self.j.review(f"VERIFY {'TIMED OUT' if code is None else f'FAILED (exit {code})'} at exchange {self.st.exchange}", f"$ {command}\n{tail}")
        return result

    def before_builder_turn(self) -> None:
        """Remember the contract hashes; filled by the planner layer."""

    def _outside_repo(self, reply: Reply) -> list[str]:
        """Writes under the owner's home outside the repo: reported, and they pause the run. Reads are only reported.

        Paths are read from the tool calls: absolute paths, ~ and $HOME, and the folders a shell command moves to with
        cd or git -C. A shell command counts as a write only where its write targets are outside (/dev/* never is).
        A relative path that the tool resolved somewhere else is invisible here (see _wrote_elsewhere)."""
        repo = os.path.realpath(self.cfg.project.repo)
        home = os.path.realpath(Path.home())
        roots = tuple({"/Users/", home + os.sep})
        variants = [repo, str(self.cfg.project.repo), repo.removeprefix("/private")]

        def outside(path: str) -> bool:
            real = os.path.realpath(path)
            if not (path.startswith(roots) or real.startswith(roots)):
                return False
            return not (real == repo or real.startswith(repo + os.sep))

        writes, reads = [], []
        for call in reply.tool_calls:
            name = call.name.lower()
            text = _mask_repo(_expand_home(call.summary, home), variants)
            found = [p for p in OUTSIDE_PATH.findall(text) if outside(p)]
            if name in SHELL_TOOLS:
                found += [d for d in _shell_dirs(call.summary, repo, home) if outside(d)]
                targets = _shell_write_targets(call.summary, repo, home)
                if targets is None:
                    mutating = bool(found) and bool(SHELL_WRITE.search(call.summary))
                else:
                    mutating = any(outside(t) for t in targets)
            else:
                mutating = bool(found) and name in FILE_WRITE_TOOLS
            if not (found or mutating):
                continue
            (writes if mutating else reads).append(f"{call.name}: {call.summary[:200]}")
        if reads:
            self.j.review(f"BUILDER READ OUTSIDE THE REPO at exchange {self.st.exchange}", "\n".join(reads))
        if writes:
            self.j.review(f"BUILDER WROTE OUTSIDE THE REPO at exchange {self.st.exchange}: run paused", "\n".join(writes))
        return writes

    def after_builder_audit(self, before: Snapshot, after: Snapshot) -> list[str]:
        """Contract drift during the turn, as notes for the supervisor; filled by the planner layer."""
        return []

    def _audit_builder(self, reply: Reply, before: Snapshot, after: Snapshot) -> None:
        patterns = list(DEFAULT_DANGER) + [(p, "safety.danger_commands") for p in self.cfg.safety.danger_commands]
        if self.cfg.git_push == "never":
            patterns.append((PUSH, "git push while git.push = never"))
        for call in reply.tool_calls:
            if call.name.lower() not in SHELL_TOOLS:
                continue
            for pattern, label in patterns:
                if pattern.search(call.summary):
                    self.j.review(f"DANGER COMMAND ({label}) at exchange {self.st.exchange}", call.summary)
        _, email = self.repo.local_identity()
        for commit in self.repo.new_commits(before.head, after.head):
            problems = attribution_problems(commit)
            if problems:
                self.j.review(f"COMMIT ATTRIBUTION in {commit.short}", f"{commit.subject}\n" + "\n".join(problems))
            if email and commit.author_email != email:
                self.j.review(
                    f"COMMIT AUTHOR in {commit.short}",
                    f"author {commit.author_name} <{commit.author_email}> differs from the repo's local user.email <{email}>",
                )
        if self.cfg.git_push == "never" and before.remotes != after.remotes:
            moved = sorted(set(before.remotes.items()) ^ set(after.remotes.items()))
            self.j.review("REMOTE REFS MOVED while git.push = never (a push or a fetch)", "\n".join(f"{r} {s}" for r, s in moved))

    def _check_models(self, role: str, reply: Reply) -> None:
        expected = self.backends[role].cfg.engine_model().split("/")[-1]
        served = {m for m in reply.served_models if m}
        if not served:
            return
        wrong = sorted(m for m in served if not (m == expected or (not re.search(r"\d", expected) and expected in m)))
        if wrong:
            self.j.review(
                f"MODEL MISMATCH: {role} was served by {', '.join(wrong)}, not {expected}",
                f"session {reply.session_id}, exchange {self.st.exchange}",
            )

    # ------------------------------------------------------------------ supervisor

    def supervisor_message(self, *, new_session: bool) -> tuple[str, list[Block]]:
        cfg, st = self.cfg, self.st
        review = st.review or {}
        enforcement = self.enforcement["supervisor"]
        blocks: list[Block] = []
        if new_session:
            blocks.append(Block("bridge", prompts.supervisor_role(cfg, enforcement, self._supervisor_rules_text())))
            reason = st.handoff.get("supervisor")
            if st.exchange > 1 and st.last_verdict:
                blocks.append(Block("bridge", prompts.supervisor_handoff(st.exchange - 1, st.last_verdict.get("text"), reason or "a new session")))
        else:
            blocks.append(Block("bridge", prompts.supervisor_reminder(cfg, enforcement)))
        blocks.append(Block("bridge", prompts.REVERIFY))
        blocks.append(Block("bridge", self._since_text(review)))
        for note in review.get("notes", []):
            blocks.append(Block("bridge", note))
        for note in st.supervisor_notes:
            blocks.append(Block("bridge", note))
        for text in review.get("planner", []):
            blocks.append(Block("planner", text))
        if review.get("wait"):
            blocks.append(Block("bridge", f"The builder asked for: {review['wait']}. It is honoured after your reply unless you write NO WAIT or a different WAIT line."))
        if review.get("wait_error"):
            blocks.append(Block("bridge", f"The builder wrote a WAIT line the bridge could not use: {review['wait_error']}"))
        if review.get("verify"):
            blocks.append(Block("bridge", prompts.verify_note(review["verify"])))
        if review.get("decisions"):
            blocks.append(Block("bridge", f"The builder listed {len(review['decisions'])} DECISIONS NEEDED; answer each in your REPLY."))
        for claim in review.get("phase_claims", [])[:1]:
            blocks.append(Block("bridge", f'The builder says "{claim}". If you verify that phase\'s exit criteria, write PHASE COMPLETE: <phase>.'))
        unchanged = st.counters.get("unchanged", 0)
        if unchanged >= IDLE_NUDGE_AFTER:
            blocks.append(Block("bridge", f"The last {unchanged} exchanges changed nothing in the repo. If the builder is waiting for a job or a time, use a WAIT directive instead of acknowledging."))
        if "builder" in st.rotate:
            blocks.append(Block("bridge", f"The builder's next message opens a fresh session ({st.rotate['builder']}); make your REPLY self-contained."))
        blocks.extend(self.supervisor_contract_blocks())
        for item in st.owner_queue:
            if item["to_supervisor"] and not item["shown"]:
                if not item["to_builder"]:
                    note = "(verbatim; binding; for you)"
                elif item["delivered"]:
                    note = "(verbatim; binding; already delivered to the builder)"
                else:
                    note = "(verbatim; binding; delivered to the builder with your REPLY)"
                blocks.append(Block("owner", item["text"], note=note))
        if st.feed:
            told = render([Block(f["origin"], f["text"]) for f in st.feed]).strip()
            blocks.append(Block("bridge", f"Besides your last REPLY, the builder was told:\n{told}"))
        report = Path(review["report"]).read_text(encoding="utf-8", errors="replace") if review.get("report") else ""
        if len(report) > REPORT_CHARS:
            report = f"[bridge] (the first {len(report) - REPORT_CHARS:,} characters are omitted; full text: {review['report']})\n" + report[-REPORT_CHARS:]
        message = render(blocks) + f"===== BUILDER REPORT (exchange {review.get('exchange', st.exchange)}) =====\n{report.strip()}\n===== END =====\n"
        return message, blocks

    def supervisor_contract_blocks(self) -> list[Block]:
        """The blocked set and contract drift; filled by the planner layer."""
        return []

    def _supervisor_rules_text(self) -> str | None:
        path = self.cfg.project.supervisor_rules
        if path and path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def _since_text(self, review: dict[str, Any]) -> str:
        then, now_head = self.st.supervisor_head, self.repo.head()
        if then and now_head and then != now_head:
            commits = self.repo.new_commits(then, now_head)
            subjects = "; ".join(c.subject for c in commits[:8]) + ("; ..." if len(commits) > 8 else "")
            stat = self.repo.diff_summary(then, now_head)
            head = f"HEAD {then[:7]}..{now_head[:7]} ({len(commits)} commits: {subjects}){chr(10) + stat if stat else ''}"
        else:
            head = f"HEAD unchanged at {now_head[:7]}" if now_head else "no commits yet"
        changed = len(self.repo.status())
        dur = review.get("duration_s")
        turn = f" Builder turn: {int(dur // 60)}m{int(dur % 60):02d}s." if dur is not None else ""
        return f"Since your last turn: {head}; working tree: {changed} uncommitted path(s).{turn}"

    def _supervisor_turn(self) -> int | None:
        st = self.st
        backend = self.backends["supervisor"]
        review = st.review or {}
        if not review.get("counted"):
            st.exchange += 1
            review["exchange"] = st.exchange
            review["counted"] = True
            st.review = review
        rotate_reason = st.rotate.pop("supervisor", None)
        if rotate_reason:
            self.retire_session("supervisor", rotate_reason)
            st.handoff["supervisor"] = rotate_reason
        # An adopted session (pin, or the old bridge's) has never seen this bridge's role text.
        refresh = bool((st.sessions.get("supervisor") or {}).get("needs_role"))
        new_session = backend.session_id is None or refresh
        if new_session:
            backend.system_prompt = prompts.supervisor_role(self.cfg, self.enforcement["supervisor"], self._supervisor_rules_text())
        message, _ = self.supervisor_message(new_session=new_session)
        review["in_flight"] = True
        self.save()
        out = self._ask_supervisor(message)
        if st.review:
            st.review["in_flight"] = False
        if out is None or isinstance(out, int):
            self.save()
            return out
        st.handoff.pop("supervisor", None)
        if (st.sessions.get("supervisor") or {}).get("needs_role"):
            st.sessions["supervisor"]["needs_role"] = False
        st.supervisor_notes = []
        for item in st.owner_queue:
            if item["to_supervisor"]:
                item["shown"] = True
        self._prune_owner_queue()
        for key in ("timeouts_supervisor", "questions_supervisor", "errors_supervisor", "limit_sleep_s", "limit_unknown_supervisor"):
            st.counters.pop(key, None)
        if self._last_supervisor_context and self._last_supervisor_context >= self.cfg.rotation.supervisor_max_context_tokens:
            st.rotate["supervisor"] = f"context reached {self._last_supervisor_context:,} tokens"
        return self._apply_supervisor(out)

    def _ask_supervisor(self, message: str) -> SupervisorOutput | int | None:
        """Send, check the tripwire, parse, and nudge once for each malformed shape."""
        n = self.st.exchange
        self.j.transcript(f"EXCHANGE {n} | to supervisor ({self.backends['supervisor'].describe()})", message)
        nudges: list[str] = self.st.review.setdefault("nudges", []) if self.st.review else []
        text = ""
        for _ in range(5):
            before = self.repo.snapshot()
            try:
                reply = self._send("supervisor", message)
            except BackendError as e:
                return self._turn_failed("supervisor", e)
            after = self.repo.snapshot()
            self._last_supervisor_context = reply.context_tokens
            self._tripwire("supervisor", reply, before, after)
            self._check_models("supervisor", reply)
            text = select_text(reply, ("REPLY:", "PROJECT COMPLETE"))
            self.j.transcript(f"EXCHANGE {n} | supervisor reply", reply.text)
            out = parse_supervisor(text, now=self.now())
            if not text.strip():
                nudge = "empty"
            elif out.near_misses and not out.complete:
                nudge = "near_miss"
            elif not out.has_reply and not out.complete:
                nudge = "no_reply"
            elif out.scope is None and not out.complete:
                nudge = "no_scope"
            else:
                nudge = self.scope_violation(out)
            if nudge is None:
                return out
            if nudge in nudges:
                return self._malformed(nudge, out)
            nudges.append(nudge)
            self.save()
            message = render([Block("bridge", self._nudge_text(nudge, out))])
            self.j.transcript(f"EXCHANGE {n} | to supervisor (nudge: {nudge})", message)
        return self._malformed("repeated", parse_supervisor(text, now=self.now()))

    def _nudge_text(self, nudge: str, out: SupervisorOutput) -> str:
        fixed = {"empty": prompts.EMPTY_NUDGE, "no_reply": prompts.NO_REPLY_NUDGE, "no_scope": prompts.NO_SCOPE_NUDGE}
        if nudge == "near_miss":
            return prompts.near_miss_nudge(out.near_misses)
        return fixed.get(nudge) or self.scope_nudge_text(out)

    def scope_violation(self, out: SupervisorOutput) -> str | None:
        """'scope' when SCOPE touches the blocked set; filled by the planner layer."""
        return None

    def scope_nudge_text(self, out: SupervisorOutput) -> str:
        return prompts.NO_SCOPE_NUDGE

    def _malformed(self, nudge: str, out: SupervisorOutput) -> SupervisorOutput | int | None:
        if nudge == "empty":
            return self._turn_failed("supervisor", TransientError("supervisor returned no output twice"))
        if nudge == "no_reply":
            self.j.review(f"SUPERVISOR REPLY WITHOUT REPLY: at exchange {self.st.exchange}", "the whole output was sent to the builder (C5)")
            out.reply = out.raw.strip()
            return out
        if nudge == "no_scope":
            self.j.review(f"SUPERVISOR REPLY WITHOUT SCOPE at exchange {self.st.exchange}", "sent anyway after one nudge")
            return out
        if nudge == "near_miss":
            self.j.review(f"SUPERVISOR OUTPUT NEAR-MISS at exchange {self.st.exchange}", "\n".join(out.near_misses))
            if not out.has_reply and not out.complete:
                out.reply = out.raw.strip()
            return out
        if nudge.startswith("scope"):
            return self.scope_refused(out)
        return self.pause("error", "the supervisor's output stayed malformed after one nudge for each problem")

    def scope_refused(self, out: SupervisorOutput) -> int:
        return EXIT_PAUSED

    def _tripwire(self, role: str, reply: Reply, before: Snapshot, after: Snapshot) -> None:
        if before.fingerprint() != after.fingerprint():
            paths = self.repo.changed_paths(before, after)
            self.j.review(
                f"{role.upper()} TURN CHANGED THE REPO (session {reply.session_id}, exchange {self.st.exchange})",
                "\n".join(paths) + "\n(a background job or the owner may also have written; the tool calls below show what the agent did)",
            )
        writes = [c for c in reply.tool_calls if c.mutating]
        if writes:
            self.j.review(
                f"{role.upper()} USED WRITE TOOLS (session {reply.session_id}, exchange {self.st.exchange})",
                "\n".join(f"{c.name}: {c.summary}" for c in writes),
            )

    def _apply_supervisor(self, out: SupervisorOutput) -> int | None:
        st = self.st
        n = st.exchange
        review = st.review or {}
        verdict = out.verdict or "(no verdict)"
        st.last_verdict = {"exchange": n, "text": verdict, "at": iso(self.now())}
        self.j.event("verdict", exchange=n, verdict=verdict, scope=out.scope_text, complete=out.complete)
        self.j.console(f"[{self.now():%H:%M:%S}] exchange {n} VERDICT: {verdict}")
        for warning in out.warnings:
            self.j.review(f"SUPERVISOR OUTPUT at exchange {n}", warning)
        st.feed = []
        st.supervisor_head = self.repo.head()
        if out.complete:
            return self._complete(out)
        if out.escalate and self.cfg.project.mode == "escalate":
            st.pending = new_pending(supervisor=out.reply)
            st.review = None
            return self.pause("escalation", out.escalate, resume="BUILDER_TURN")
        handled = self.handle_directives(out)
        if handled == "again":
            self.save()
            return None
        if handled is not None:
            return handled
        if st.phase != "SUPERVISOR_TURN":
            self.save()
            return None
        reply_text = out.reply or ""
        note = ""
        if self.confirm is not None:
            decided = self.confirm(verdict, reply_text)
            if decided is None:
                self.save()
                self.j.console("Discarded by the owner; the supervisor drafts again on the next run.")
                return EXIT_OK
            if decided != reply_text:
                reply_text, note = decided, "(edited by the owner)"
        pending = new_pending(supervisor=reply_text, supervisor_note=note)
        st.review = None
        self._replies_this_run += 1
        if out.phase_complete:
            self._phase_complete(out.phase_complete)
        if out.rotate_builder:
            st.rotate["builder"] = "ROTATE BUILDER from the supervisor"
        spec = out.wait
        if spec is None and review.get("wait") and not out.no_wait:
            try:
                spec = parse_wait_line(review["wait"], now=self.now())
            except ValueError:
                spec = None
        if spec is not None:
            return self._begin_wait(spec, "supervisor" if out.wait else "builder", pending)
        st.pending = pending
        st.phase = "BUILDER_TURN"
        self.save()
        return None

    def handle_directives(self, out: SupervisorOutput) -> int | str | None:
        """REPLAN; filled by the planner layer. None: deliver the reply; "again": the supervisor goes again."""
        return None

    def _phase_complete(self, phase: str) -> None:
        st = self.st
        st.phases_done.append({"phase": phase, "exchange": st.exchange, "at": iso(self.now())})
        self.j.review(f"PHASE COMPLETE: {phase} (verified by the supervisor at exchange {st.exchange})")
        self.j.event("phase_complete", phase=phase, exchange=st.exchange)
        if self.cfg.rotation.builder_on_phase_complete:
            st.rotate["builder"] = f"{phase} complete"
        if self.cfg.rotation.supervisor_on_phase_complete:
            st.rotate["supervisor"] = f"{phase} complete"
        self.on_phase_complete(phase)

    def on_phase_complete(self, phase: str) -> None:
        """The ledger entry; filled by the planner layer."""

    def _complete(self, out: SupervisorOutput) -> int:
        st = self.st
        summary = out.completion_text()
        st.complete = {"at": iso(self.now()), "exchange": st.exchange, "summary": summary}
        st.phase = "COMPLETE"
        st.review = None
        sup = st.sessions.get("supervisor") or {}
        st.sessions["supervisor"] = {**sup, "closed": True}
        self.save()
        self.j.review(f"PROJECT COMPLETE at exchange {st.exchange}", summary)
        self.j.event("complete", exchange=st.exchange)
        self.notify("complete", "PROJECT COMPLETE", summary.splitlines()[0] if summary else f"at exchange {st.exchange}")
        self.j.console(f"PROJECT COMPLETE at exchange {st.exchange}")
        if self.on_complete:
            self.on_complete(self)
        return EXIT_OK

    # ------------------------------------------------------------------ waits and sleeps

    def _begin_wait(self, spec: Any, source: str, pending: dict[str, Any]) -> int | None:
        st = self.st
        active = begin_wait(spec, now=self.now(), source=source, wait_max=self.cfg.wait_max, pid_start=self.pid_start)
        st.wait = {"active": active.to_json(), "pending": pending}
        st.pending = None
        st.phase = "WAITING"
        self.save()
        if active.clamped:
            self.j.review(f"WAIT CLAMPED at exchange {st.exchange}", f"{spec.describe()} capped at waits.max; it ends at {active.deadline}")
        self.j.event("wait_start", spec=spec.describe(), source=source, deadline=active.deadline)
        self.j.console(f"[{self.now():%H:%M:%S}] {spec.describe()} (asked by the {source}; ends by {active.deadline}): sleeping, no model calls")
        return None

    def _waiting(self) -> int | None:
        st = self.st
        active = ActiveWait.from_json(st.wait["active"])
        spec = active.spec.describe()
        while True:
            if self.sd.stop_requested():
                return None
            self.take_inbox()
            if st.phase != "WAITING" or not st.wait:
                return None
            if st.wait.get("interrupted"):
                note = prompts.wait_interrupted_note(spec)
                outcome = "interrupted by an owner message"
                break
            result = check_wait(active, now=self.now(), repo=self.cfg.project.repo, pid_start=self.pid_start)
            if result.done:
                note = prompts.wait_over_note(spec, result.detail, result.met)
                outcome = result.detail
                break
            remaining = (datetime.fromisoformat(active.deadline) - self.now()).total_seconds()
            self.clock.sleep(max(1.0, min(WAIT_POLL, remaining)))
        pending = st.wait.get("pending") or new_pending()
        set_note(pending, "wait", note)
        st.pending = pending
        st.wait = None
        st.phase = "BUILDER_TURN"
        st.counters["unchanged"] = 0
        self.save()
        self.j.event("wait_end", spec=spec, outcome=outcome)
        self.j.console(f"[{self.now():%H:%M:%S}] wait over: {outcome}")
        return None

    def _sleep_until(self, until: datetime, reason: str, *, resume: str) -> int | None:
        st = self.st
        st.sleep = {"until": iso(until), "reason": reason, "resume": resume}
        st.phase = "SLEEPING"
        self.save()
        self.j.event("sleep_start", until=iso(until), reason=reason, resume=resume)
        self.j.console(f"[{self.now():%H:%M:%S}] sleeping until {until:%Y-%m-%d %H:%M} ({reason}); no model calls")
        return None

    def _sleeping(self) -> int | None:
        st = self.st
        until = datetime.fromisoformat(st.sleep["until"])
        while self.now() < until:
            if self.sd.stop_requested():
                return None
            self.clock.sleep(min(SLEEP_STEP, (until - self.now()).total_seconds()))
        resume = st.sleep.get("resume") or "IDLE"
        self.j.event("sleep_end", reason=st.sleep.get("reason"))
        st.sleep = None
        st.phase = resume
        self.save()
        return None

    # ------------------------------------------------------------------ sending and failures

    def _send(self, role: str, message: str) -> Reply:
        backend = self.backends[role]
        raw = self.sd.turns / f"{self.st.exchange:04d}-{role}.jsonl"
        started = self.now()
        self.j.event("turn_start", role=role, exchange=self.st.exchange, engine=backend.describe(), session=backend.session_id)

        def on_event(ev: Event) -> None:
            self.j.event("agent", role=role, type=ev.kind, text=ev.text[:2000], tool=ev.tool)

        try:
            reply = backend.send(
                message,
                timeout=backend.cfg.timeout,
                on_event=on_event,
                cancel=_StopNow(self.sd),
                on_session=lambda sid: self._record_session(role, sid),
                raw_path=raw,
            )
        except BackendError:
            raise
        except BaseException:
            # Ctrl-C or SIGTERM: stop the turn where it runs (opencode's server keeps going otherwise).
            try:
                backend.abort()
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
            raise
        self._account(role, reply)
        self.j.event(
            "turn_end",
            role=role,
            exchange=self.st.exchange,
            seconds=round((self.now() - started).total_seconds(), 1),
            session=reply.session_id,
            models=sorted(reply.served_models),
            context_tokens=reply.context_tokens,
            cost_usd=reply.cost_usd,
            usage=reply.usage or None,
        )
        self._prune_turn_files()
        return reply

    def _account(self, role: str, reply: Reply) -> None:
        """Usage per role and in total, at the engine's API prices (on a subscription this is not what is billed)."""
        u = self.st.usage
        u["turns"] = u.get("turns", 0) + 1
        if reply.cost_usd is not None:
            u["cost_usd"] = round(u.get("cost_usd", 0.0) + reply.cost_usd, 6)
            self._run_cost += reply.cost_usd
        per = u.setdefault("roles", {}).setdefault(role, {})
        per["turns"] = per.get("turns", 0) + 1
        if reply.cost_usd is not None:
            per["cost_usd"] = round(per.get("cost_usd", 0.0) + reply.cost_usd, 6)
        for key, value in (reply.usage or {}).items():
            per[key] = per.get(key, 0) + int(value)

    def _prune_turn_files(self, keep: int = 400) -> None:
        files = sorted(self.sd.turns.glob("*"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep] if len(files) > keep else []:
            p.unlink(missing_ok=True)

    def _bump(self, key: str) -> int:
        self.st.counters[key] = self.st.counters.get(key, 0) + 1
        return self.st.counters[key]

    def _backoff(self, role: str, reason: str, *, start: int = BACKOFF_START, cap: int = BACKOFF_MAX) -> int | None:
        n = self.st.counters.get(f"errors_{role}", 1)
        seconds = min(start * 2 ** max(0, n - 1), cap)
        return self._sleep_until(self.now() + timedelta(seconds=seconds), f"{role}: {reason}; retry in {format_duration(seconds)}", resume=self.st.phase)

    def _turn_failed(self, role: str, e: BackendError) -> int | None:
        st = self.st
        kind = type(e).__name__
        self.j.event("turn_error", role=role, error=kind, message=str(e))
        self.j.console(f"[{self.now():%H:%M:%S}] {role}: {kind}: {str(e)[:300]}")
        pending = st.pending if role == "builder" else None
        if isinstance(e, Unsupported):
            raise FatalError(f"{role}: {e}")
        if isinstance(e, BillingFailed):
            return self.pause("billing", f"{role}: {e}. The account has no credit for this turn; add credit or change billing, then `agent-bridge run`.")
        if isinstance(e, AuthFailed):
            return self.pause("auth", f"{role}: {e}. Run `claude login` (or re-enroll the pool account), then `agent-bridge run`.")
        if isinstance(e, Cancelled):
            if pending is not None:
                set_note(pending, "resume", "The owner stopped the bridge (stop --now) during your previous turn, and the bridge aborted it. Check git status and git log before continuing; do not redo committed work.")
            elif st.review is not None:
                st.review["notes"] = ["The owner stopped the bridge (stop --now) during your previous turn; review the report below again."]
            self.save()
            return None
        if isinstance(e, (SessionLimit, RateLimited)):
            reset = e.reset_at
            if reset is None and isinstance(e, SessionLimit):
                n = self._bump(f"limit_unknown_{role}")
                reset = self.now() + timedelta(seconds=min(LIMIT_UNKNOWN_START * 2 ** (n - 1), LIMIT_UNKNOWN_MAX))
            if reset is not None:
                until = max(reset, self.now()) + timedelta(seconds=RESET_MARGIN)
                slept = st.counters.get("limit_sleep_s", 0) + (until - self.now()).total_seconds()
                if slept > LIMIT_SLEEP_CAP:
                    return self.pause("limits", f"{role}: usage limits kept the run asleep for more than 48 hours; last: {e}")
                st.counters["limit_sleep_s"] = slept
                if pending is not None:
                    set_note(pending, "limit", prompts.limit_note(f"{self.now():%H:%M}", str(e)[:200]))
                self.j.review(f"USAGE LIMIT ({role}) at exchange {st.exchange}", f"{e}\nsleeping until {until:%Y-%m-%d %H:%M}")
                return self._sleep_until(until, f"{role}: usage limit until {reset:%Y-%m-%d %H:%M}", resume=st.phase)
            self._bump(f"errors_{role}")
            return self._backoff(role, f"rate limited: {e}")
        if isinstance(e, Timeout):
            n = self._bump(f"timeouts_{role}")
            if n >= MAX_TIMEOUTS:
                return self.pause("repeated timeouts", f"{role}: {n} timeouts in a row (each {format_duration(self.backends[role].cfg.timeout)}); last: {e}")
            if pending is not None:
                set_note(pending, "resume", prompts.resume_note(self.backends[role].cfg.timeout))
                self.save()
                return None
            self._bump(f"errors_{role}")
            return self._backoff(role, f"timed out: {e}")
        if isinstance(e, QuestionAsked):
            n = self._bump(f"questions_{role}")
            if n >= MAX_QUESTIONS:
                return self.pause("question tool", f"{role}: called an interactive question tool {n} times in a row: {e.questions}")
            self.j.review(f"QUESTION TOOL ABORTED ({role}, {n} in a row)", "\n".join(e.questions))
            if pending is not None:
                set_note(pending, "question", prompts.question_note(e.questions))
            elif st.review is not None:
                st.review["notes"] = ["Your previous turn called an interactive question tool; nobody can answer it, so the bridge rejected it and aborted the turn. Never use it: decide, or put the question in your REPLY."]
            self.save()
            return None
        n = self._bump(f"errors_{role}")
        if n >= MAX_ERRORS:
            return self.pause("repeated errors", f"{role}: {n} errors in a row; last: {kind}: {e}")
        return self._backoff(role, f"{kind}: {str(e)[:200]}")

    # ------------------------------------------------------------------ stop and pause

    def unsent_message(self) -> str | None:
        st = self.st
        pending = st.pending
        if pending is None and st.wait:
            pending = st.wait.get("pending")
        if not pending or not (pending.get("supervisor") or pending.get("planner") or pending.get("notes")):
            if not any(i["to_builder"] and not i["delivered"] for i in st.owner_queue):
                return None
        saved = st.pending
        st.pending = pending or new_pending()
        try:
            return self.builder_message()[0]
        finally:
            st.pending = saved

    def _stop(self) -> int:
        unsent = self.unsent_message()
        if unsent:
            atomic_write_text(self.sd.unsent, unsent)
        detail = f"STOP file found; unsent message saved to {self.sd.unsent}" if unsent else "STOP file found"
        self.pause("stop", detail, resume=self.st.phase)
        self.j.console(f"Stopped at exchange {self.st.exchange}." + (f" Unsent message: {self.sd.unsent}" if unsent else ""))
        return EXIT_OK

    def notify(self, kind: str, title: str, message: str) -> None:
        """Tell the owner something needs them (desktop and/or [notify] command). Never fails the run."""
        if self.notifier is None:
            return
        try:
            self.notifier(kind, f"{self.cfg.project.name}: {title}", message[:400])
        except Exception as e:  # noqa: BLE001
            self.j.event("notify_failed", error=str(e))

    def pause(self, reason: str, detail: str, *, resume: str | None = None) -> int:
        st = self.st
        resume_phase = resume or st.phase
        if resume_phase == "PAUSED":
            resume_phase = (st.pause or {}).get("resume", "IDLE")
        st.pause = {"reason": reason, "detail": detail, "resume": resume_phase, "at": iso(self.now())}
        st.phase = "PAUSED"
        self.save()
        atomic_write_text(self.sd.paused, self.pause_summary())
        if reason != "stop":
            self.j.review(f"PAUSED ({reason}) at exchange {st.exchange}", detail)
            self.notify("paused", f"paused: {reason}", detail)
        self.j.event("paused", reason=reason, detail=detail, resume=resume_phase)
        self.j.console(f"PAUSED ({reason}): {detail}")
        return EXIT_OK if reason == "stop" else EXIT_PAUSED

    def pause_summary(self) -> str:
        st = self.st
        pause = st.pause or {}
        lines = [
            f"# Paused: {pause.get('reason')}",
            "",
            f"- When: {pause.get('at')}",
            f"- Why: {pause.get('detail')}",
            f"- Exchange: {st.exchange}",
            f"- Resumes in: {pause.get('resume')}",
            f"- Last verdict: {(st.last_verdict or {}).get('text', '(none)')}",
            f"- Counters: {st.counters}",
        ]
        if st.review and st.review.get("decisions"):
            lines.append("- Open DECISIONS NEEDED from the builder:")
            lines += [f"  - {d}" for d in st.review["decisions"]]
        if self.sd.unsent.exists():
            lines.append(f"- Unsent message: {self.sd.unsent}")
        lines += ["", "Continue with: `agent-bridge run --forever` (it resumes from the saved state)."]
        return "\n".join(lines) + "\n"


class _StopNow:
    def __init__(self, sd: StateDir) -> None:
        self.sd = sd

    def is_set(self) -> bool:
        return self.sd.stop_now_requested()
