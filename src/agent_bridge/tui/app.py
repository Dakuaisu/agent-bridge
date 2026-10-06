"""The terminal UI behind `agent-bridge` with no arguments.

Every action runs an agent-bridge CLI command as a detached process, so the view can be closed and
reopened at any time without touching a running bridge.
"""

from __future__ import annotations

import curses
import locale
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from agent_bridge import contract, registry
from agent_bridge.config import ENGINES, ROLES
from agent_bridge.live import render_event
from agent_bridge.protocol import display_token
from agent_bridge.tui import draw as D
from agent_bridge.tui import theme as T
from agent_bridge.tui.canvas import Canvas
from agent_bridge.tui.jobs import Job, Runner, payload
from agent_bridge.tui.modals import (
    Button,
    ChoiceField,
    Command,
    Confirm,
    DocField,
    Form,
    LineField,
    Modal,
    NoteField,
    Palette,
    Picker,
    Tab,
    TextField,
    ToggleField,
    Viewer,
    todo_detail,
)
from agent_bridge.tui.model import PLANNER_PHASES, ProjectRow, ProjectWatcher, awaiting_answers, summarize
from agent_bridge.tui.text import one_line, tilde
from agent_bridge.tui.theme import INHERIT, Style, nearest_basic
from agent_bridge.tui.widgets import ListState, keyname

MIN_W, MIN_H = 60, 16
HAIKU = "claude-haiku-4-5-20251001"
PRESETS: list[tuple[str, dict[str, str] | None]] = [
    ("Claude Code · default models (Fable plans and reviews, Opus builds)", {}),
    ("Claude Code · Haiku everywhere (a cheap test run)", {r: f"claude-code:{HAIKU}" for r in ROLES}),
    ("opencode builder · Claude Code planner and supervisor", {"builder": "opencode:anthropic/claude-opus-5-5"}),
    (
        "opencode everywhere (read-only by instruction only)",
        {"planner": "opencode:anthropic/claude-fable-5-1", "supervisor": "opencode:anthropic/claude-fable-5-1", "builder": "opencode:anthropic/claude-opus-5-5"},
    ),
    ("Custom engines and models", None),
]
DEFAULT_ROLE = {"planner": "claude-code:claude-fable-5-1", "supervisor": "claude-code:claude-fable-5-1", "builder": "claude-code:claude-opus-5-5"}

HELP = """AGENT·BRIDGE

One command: `agent-bridge` opens this view on the current folder's project. ctrl-a lists every project.

DASHBOARD
  r      run, or resume, the loop
  s      stop: after the current step, or abort the turn now
  m      message the agents: your words, verbatim, labelled [owner]
  i      answer the planner's questions
  a      approve: the drafted plan, a plan change, or the contract
  v      read the plan: PRD, rules, ledger, open items, bridge.toml
  d      apply an owner decisions doc through the planner
  o      the owner to-do: decisions to review, OWNER-BLOCKED items
  p      the owner-review report
  l      logs: transcript, alerts, console, events
  ↑ ↓ PgUp PgDn   scroll the stream; End follows it live again
  w      wrap long stream lines
  :      every command (also ctrl-p)
  ctrl-a all projects
  ?      this help
  q      quit the view. The bridge keeps running; `agent-bridge` brings you back.

FORMS
  tab or ↓ next field · ctrl-s submit · ctrl-e edit the text in $EDITOR · esc cancel

VIEWERS
  ↑ ↓ scroll · tab switch file · / search · n next match · e edit in $EDITOR · w wrap · esc close

HOW IT WORKS
  Each action is an agent-bridge CLI command started in the background (run, say, approve, decide,
  stop…). Its output is kept under ~/.local/state/agent-bridge/ui/, and the result shows as a toast.
  The view only reads .bridge/; closing it never stops a run.
"""


def _git_value(key: str) -> str:
    try:
        out = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _read(path: Path, limit: int = 600_000) -> str:
    if not path.exists():
        return f"({path.name} does not exist yet)"
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > limit:
            f.seek(size - limit)
            data = f.read()
            return "(earlier content not shown)\n" + data.split(b"\n", 1)[-1].decode("utf-8", "replace")
        return f.read().decode("utf-8", "replace")


class App:
    def __init__(self, folder: Path, *, show_all: bool = False, runner: Runner | None = None, splash: bool = True) -> None:
        self.here = Path(folder).expanduser().resolve()
        self.runner = runner or Runner()
        self.view = D.View()
        self.modals: list[Modal] = []
        self.toast_items: list[tuple[str, str, float]] = []
        self.quit = False
        self.dirty = True
        self.editor: tuple[str, Any, Callable[..., Any] | None] | None = None
        self.welcome = ListState()
        self.plist = ListState()
        self.rows: list[ProjectRow] = []
        self._rows_at = 0.0
        self._refresh_at = 0.0
        self._error_at = 0.0
        self.starting_until = 0.0
        self.open(self.here)
        self.home_root = self.snap.root
        self.screen = "projects" if show_all else "project"
        now = time.monotonic()
        self.splash_start = now
        self.splash_until = now + 1.1 if splash else 0.0

    # ------------------------------------------------------------- project

    def open(self, folder: Path) -> None:
        self.watcher = ProjectWatcher(folder)
        self.snap = self.watcher.refresh()
        self.view.scroll = 0
        self.welcome.index = 0
        self._refresh_at = 0.0
        if self.snap.configured:
            registry.remember(self.snap.root, self.snap.name)

    @property
    def repo(self) -> Path:
        return self.snap.root

    def project_rows(self) -> list[ProjectRow]:
        rows = []
        here = self.home_root.resolve()
        known: dict[str, dict[str, Any]] = {}
        for p in registry.projects():
            known.setdefault(str(Path(p["repo"]).resolve()), p)
        rows.append(summarize(here, known.get(str(here), {}).get("name") or here.name, here=True))
        for repo, p in known.items():
            if repo != str(here):
                rows.append(summarize(Path(repo), p.get("name") or Path(repo).name))
        return rows

    # ------------------------------------------------------------- loop hooks

    def tick(self) -> None:
        now = time.monotonic()
        if now >= self._refresh_at:
            self._refresh_at = now + 0.4
            try:
                self.snap = self.watcher.refresh()
            except Exception as e:  # noqa: BLE001 - a bad file must not take the view down
                self._read_error(e)
        for job in self.runner.poll():
            self.finished(job)
        if self.screen == "projects" and now >= self._rows_at:
            self._rows_at = now + 1.5
            try:
                self.rows = self.project_rows()
            except Exception as e:  # noqa: BLE001
                self._read_error(e)
        self.toast_items = [t for t in self.toast_items if t[2] > now]
        self.view.jobs = [j.label for j in self.runner.busy()]
        self.view.toasts = [(t[0], t[1]) for t in self.toast_items]
        self.view.starting = now < self.starting_until

    def _read_error(self, e: Exception) -> None:
        now = time.monotonic()
        if now >= self._error_at:
            self._error_at = now + 30
            self.toast(f"✗ could not read the project: {type(e).__name__}: {e}", "bad", 10)

    def paste(self, text: str) -> None:
        """A burst of typed characters (a paste) goes into the focused text field as text: a tab or a newline in it
        must not move the focus or submit the form."""
        self.dirty = True
        if self.modals and hasattr(self.modals[-1], "paste") and self.modals[-1].paste(text):
            return
        for ch in text:
            self.key(keyname(ch))

    def animating(self) -> bool:
        return bool(time.monotonic() < self.splash_until or self.snap.holder or self.runner.jobs or self.toast_items or self.rows and any(r.holder for r in self.rows))

    def toast(self, text: str, tone: str = "info", seconds: float = 6.0) -> None:
        self.toast_items.append((text, tone, time.monotonic() + seconds))
        self.toast_items = self.toast_items[-4:]
        self.dirty = True

    def finished(self, job: Job) -> None:
        message = job.then(job) if job.then else None
        tail = job.tail(1)
        last = one_line(tail[-1]) if tail else ""
        if message:
            self.toast(message, "good" if job.code == 0 else "warn" if job.code == 3 else "bad", 9)
        elif job.code == 0:
            self.toast(f"✓ {job.label}: {last or 'done'}", "good")
        elif job.code == 3:
            self.toast(f"◆ {job.label}: {last or 'waiting for you'}", "warn", 9)
        else:
            self.toast(f"✗ {job.label} (exit {job.code}): {last or 'see the log'}", "bad", 12)
        self._refresh_at = 0.0

    def launch(self, label: str, argv: list[str], *, cwd: Path | None = None, then: Callable[[Job], str | None] | None = None, starts: bool = False) -> None:
        try:
            self.runner.start(label, argv, cwd=cwd or self.repo, then=then)
        except OSError as e:
            self.toast(f"✗ could not start {label}: {e}", "bad")
            return
        if starts:
            self.starting_until = time.monotonic() + 8
        self.toast(f"▶ {label}", "info", 3)
        self._refresh_at = 0.0

    def push(self, modal: Modal) -> None:
        self.modals.append(modal)

    def close(self, modal: Modal | None = None) -> None:
        if modal is None:
            if self.modals:
                self.modals.pop()
        elif modal in self.modals:
            self.modals.remove(modal)

    def edit_file(self, path: Path, then: Callable[[], None] | None = None) -> None:
        self.editor = ("file", path, then)

    def edit_text(self, text: str, then: Callable[[str], None]) -> None:
        self.editor = ("text", text, then)

    # ------------------------------------------------------------- drawing

    def render(self, h: int, w: int) -> Canvas:
        cv = Canvas(h, w, bg=T.CANVAS_BG)
        t = self.view.t = time.monotonic()
        if w < MIN_W or h < MIN_H:
            D.too_small(cv)
            return cv
        if t < self.splash_until:
            D.splash(cv, self.snap, t, self.splash_start)
            return cv
        if self.screen == "projects":
            self.plist.clamp(len(self.rows), max(1, h - 7))
            D.projects(cv, self.rows, self.view, self.plist.index, self.plist.top)
            items = [("⏎", "open", bool(self.rows)), ("n", "new project", True), ("+", "add a folder", True), ("x", "forget", bool(self.rows)), ("esc", "back", True), (":", "commands", True), ("?", "help", True), ("q", "quit", True)]
        elif self.snap.configured:
            D.dashboard(cv, self.snap, self.view)
            items = self.footer_items()
        else:
            self.view.selected = self.welcome.index
            D.welcome(cv, self.snap, self.view, self.welcome_options())
            items = [("↑↓", "choose", True), ("⏎", "go", True), ("^A", "projects", True), ("?", "help", True), ("q", "quit", True)]
        D.footer(cv, items, self.view)
        for m in self.modals:
            cv.cursor = None
            m.draw(cv, self, t)
        D.toasts(cv, self.view.toasts)
        return cv

    def footer_items(self) -> list[tuple[str, str, bool]]:
        s = self.snap
        items: list[tuple[str, str, bool]] = []
        if awaiting_answers(s.state):
            items.append(("i", "answer", self.why_answer() is None))
        if self.why_approve() is None:
            items.append(("a", "approve", True))
        items.append(("s", "stop", True) if s.holder else ("r", "run", self.why_run() is None))
        items += [
            ("m", "message", self.why_message() is None),
            ("v", "plan", True),
            ("d", "decide", self.why_decide() is None),
            ("o", "to-do", True),
            ("p", "report", True),
            ("l", "logs", True),
            (":", "commands", True),
            ("^A", "projects", True),
            ("?", "help", True),
            ("q", "quit", True),
        ]
        return items

    def welcome_options(self) -> list[tuple[str, str, str, bool]]:
        opts = []
        if self.snap.contract_files:
            opts.append(("i", "Set up this repo", "init and check, then adopt its PRD and CLAUDE.md", True))
        opts.append(("n", "New project", "the planner interviews you, then the agents build it", True))
        opts.append(("^A", "All projects", "every repo agent-bridge has opened on this machine", True))
        opts.append(("q", "Quit", "", True))
        return opts

    # ------------------------------------------------------------- keys

    def key(self, name: str) -> None:
        self.dirty = True
        if time.monotonic() < self.splash_until:
            self.splash_until = 0.0
            return
        if self.modals:
            self.modals[-1].key(name, self)
            return
        if name in ("ctrl-c", "ctrl-q"):
            self.quit = True
        elif name == "ctrl-a":
            self.toggle_projects()
        elif name in ("?", "f1"):
            self.act_help()
        elif name in (":", "ctrl-p"):
            self.act_palette()
        elif self.screen == "projects":
            self.projects_key(name)
        elif not self.snap.configured:
            self.welcome_key(name)
        else:
            self.dashboard_key(name)

    def dashboard_key(self, name: str) -> None:
        table: dict[str, Callable[[], None]] = {
            "r": self.act_run,
            "s": self.act_stop,
            "m": lambda: self.act_message("both"),
            "i": self.act_answer,
            "a": self.act_approve,
            "v": self.act_plan,
            "d": self.act_decide,
            "o": self.act_todo,
            "p": self.act_report,
            "l": self.act_logs,
            "c": self.act_check,
            "n": self.act_new,
            "q": self.act_quit,
        }
        if name in table:
            table[name]()
        elif name in ("up", "k"):
            self.view.scroll += 1
        elif name in ("down", "j"):
            self.view.scroll = max(0, self.view.scroll - 1)
        elif name == "pgup":
            self.view.scroll += 15
        elif name == "pgdn":
            self.view.scroll = max(0, self.view.scroll - 15)
        elif name in ("end", "G"):
            self.view.scroll = 0
        elif name in ("home", "g"):
            self.view.scroll = 1 << 30
        elif name == "w":
            self.view.wrap = not self.view.wrap

    def welcome_key(self, name: str) -> None:
        opts = self.welcome_options()
        if self.welcome.key(name, len(opts)):
            return
        if name == "enter":
            name = opts[self.welcome.index][0]
        if name == "i" and self.snap.contract_files:
            self.act_init()
        elif name == "n":
            self.act_new()
        elif name == "^A":
            self.toggle_projects()
        elif name == "q":
            self.act_quit()

    def projects_key(self, name: str) -> None:
        if self.plist.key(name, len(self.rows), page=10):
            return
        row = self.rows[self.plist.index] if self.rows else None
        if name == "enter" and row:
            if row.missing:
                self.toast(f"{tilde(row.repo)} no longer exists; x forgets it", "warn")
                return
            self.open(row.repo)
            self.screen = "project"
        elif name in ("esc", "backspace"):
            self.screen = "project"
        elif name == "n":
            self.act_new()
        elif name in ("+", "a"):
            self.act_add_folder()
        elif name == "x" and row and not row.here:
            self.push(
                Confirm(
                    "FORGET",
                    f"Remove {row.name} ({tilde(row.repo)}) from this list? Nothing on disk is touched; opening it again lists it again.",
                    [Button("Forget", lambda m, a: (registry.forget(row.repo), a.close(m), setattr(a, "_rows_at", 0.0)), "danger"), Button("Cancel", lambda m, a: a.close(m))],
                    accent=203,
                )
            )
        elif name == "q":
            self.act_quit()

    def toggle_projects(self) -> None:
        if self.screen == "projects":
            self.screen = "project"
            return
        self.screen = "projects"
        self.rows = self.project_rows()
        self._rows_at = time.monotonic() + 1.5
        self.plist.index = next((i for i, r in enumerate(self.rows) if r.repo == self.snap.root), 0)

    # ------------------------------------------------------------- availability

    def why_config(self) -> str | None:
        return None if self.snap.configured else "there is no agent-bridge project here"

    def why_busy(self) -> str | None:
        return "a command for this project is still starting" if self.runner.busy(self.repo) and not self.snap.holder else None

    def why_run(self) -> str | None:
        s, st = self.snap, self.snap.state
        if self.why_config():
            return self.why_config()
        if s.holder:
            return f"it is already running (pid {s.holder.get('pid')})"
        if self.why_busy():
            return self.why_busy()
        if awaiting_answers(st):
            return "the planner is waiting for your answers: press i"
        if st.phase == "PLAN_REVIEW":
            return "the plan waits for your approval: press a"
        if st.contract is None and st.phase not in PLANNER_PHASES:
            return "there is no approved contract yet: a adopts one"
        return None

    def why_stop(self) -> str | None:
        return None if self.snap.holder else "nothing is running"

    def why_message(self) -> str | None:
        if self.why_config():
            return self.why_config()
        if awaiting_answers(self.snap.state):
            return "the planner is waiting for answers: press i"
        return None

    def why_answer(self) -> str | None:
        if self.why_config():
            return self.why_config()
        if not awaiting_answers(self.snap.state):
            return "the planner is not waiting for answers"
        if self.snap.holder or self.why_busy():
            return "the planner is still working"
        return None

    def why_approve(self) -> str | None:
        s, st = self.snap, self.snap.state
        if self.why_config():
            return self.why_config()
        if st.phase == "PLAN_REVIEW":
            return None if not s.holder and not self.why_busy() else "wait for the running command"
        if s.waiting_changes:
            return None
        if s.holder:
            return "nothing waits for approval"
        if self.why_busy():
            return self.why_busy()
        if st.contract and s.drift:
            return None
        if st.contract is None and s.contract_files and st.phase not in (*PLANNER_PHASES, "INTERVIEW"):
            return None
        return "nothing waits for approval"

    def why_decide(self) -> str | None:
        if self.why_config():
            return self.why_config()
        if self.snap.state.contract is None:
            return "approve a contract first"
        return self.why_busy()

    def why_fresh(self) -> str | None:
        if self.why_config():
            return self.why_config()
        return "stop the bridge first" if self.snap.holder else self.why_busy()

    def guard(self, why: Callable[[], str | None]) -> bool:
        reason = why()
        if reason:
            self.toast(reason, "warn")
            return False
        return True

    # ------------------------------------------------------------- engines

    def engine_fields(self) -> tuple[ChoiceField, dict[str, LineField]]:
        preset = ChoiceField("Engines", [label for label, _ in PRESETS])
        custom = {r: LineField(r.capitalize(), DEFAULT_ROLE[r], when=lambda: preset.index == len(PRESETS) - 1) for r in ROLES}
        return preset, custom

    @staticmethod
    def role_flags(preset: ChoiceField, custom: dict[str, LineField]) -> list[str] | str:
        chosen = PRESETS[preset.index][1]
        roles = chosen if chosen is not None else {r: f.value.strip() for r, f in custom.items()}
        flags: list[str] = []
        for role, value in roles.items():
            engine, sep, model = value.partition(":")
            if not sep or engine not in ENGINES or not model:
                return f"{role}: write ENGINE:MODEL, with ENGINE one of {', '.join(ENGINES)}"
            flags += [f"--{role}", value]
        return flags

    # ------------------------------------------------------------- actions

    def act_quit(self) -> None:
        self.quit = True

    def act_help(self) -> None:
        self.push(Viewer([Tab("Keys", lambda: HELP, kind="md")], title="HELP"))

    def act_palette(self) -> None:
        self.push(Palette(self.commands()))

    def act_run(self) -> None:
        if not self.guard(self.why_run):
            return
        mode = ChoiceField("Run", ["until PROJECT COMPLETE or a pause", "a fixed number of exchanges"])
        count = LineField("Exchanges", "3", when=lambda: mode.index == 1)
        kickoff = TextField("Kickoff", rows=5, placeholder="Optional. Goes verbatim to the builder and the supervisor, labelled [owner].")
        fresh_sup = ToggleField("Start a fresh supervisor session")
        fresh_bld = ToggleField("Start a fresh builder session")

        def start(form: Form, app: App) -> None:
            argv = ["run", "--repo", str(self.repo)]
            if mode.index == 1:
                n = count.value.strip()
                if not n.isdigit() or int(n) < 1:
                    form.error = "exchanges must be a whole number of at least 1"
                    return
                argv += ["--loop", n]
            else:
                argv.append("--forever")
            if kickoff.value.strip():
                argv += ["--kickoff", payload("kickoff", kickoff.value.strip())]
            if fresh_sup.value:
                argv.append("--new-supervisor")
            if fresh_bld.value:
                argv.append("--new-builder")
            app.close(form)
            app.launch("run", argv, starts=True)

        fields = [mode, count, kickoff, fresh_sup, fresh_bld]
        self.push(Form("RUN", fields, [Button("Start", start), Button("Cancel", lambda f, a: a.close(f))], accent=84))

    def act_stop(self) -> None:
        if not self.guard(self.why_stop):
            return
        repo = str(self.repo)
        self.push(
            Confirm(
                "STOP",
                "A normal stop lets the current turn finish, saves any unsent message, and resumes with r. "
                "Stop now also aborts the running turn.",
                [
                    Button("Stop after this step", lambda m, a: (a.close(m), a.launch("stop", ["stop", "--repo", repo]))),
                    Button("Stop now", lambda m, a: (a.close(m), a.launch("stop now", ["stop", "--now", "--repo", repo])), "danger"),
                    Button("Cancel", lambda m, a: a.close(m)),
                ],
                accent=203,
            )
        )

    def act_message(self, target: str = "both") -> None:
        if not self.guard(self.why_message):
            return
        targets = ["both", "builder", "supervisor"]
        to = ChoiceField("To", ["both agents", "the builder only", "the supervisor only"], targets.index(target))
        text = TextField("Message", rows=7, placeholder="Delivered verbatim, labelled [owner], at the next step boundary. It also ends a wait.")
        queued = NoteField(lambda: "Nothing is running, so it waits until the next run." if not self.snap.holder else "The bridge is running: it arrives at the next step boundary.", T.FAINT)

        def send(form: Form, app: App) -> None:
            if not text.value.strip():
                form.error = "write a message first"
                return
            argv = ["say", payload("message", text.value.strip()), "--to", targets[to.index], "--repo", str(self.repo)]
            app.close(form)
            app.launch("message", argv)

        self.push(Form("MESSAGE", [to, text, queued], [Button("Send", send), Button("Cancel", lambda f, a: a.close(f))], accent=214, focus=1))

    def act_answer(self) -> None:
        if not self.guard(self.why_answer):
            return
        st = self.snap.state
        questions = self.snap.questions.strip() or "(The planner's questions are in .bridge/plan/.)"
        if st.phase == "PAUSED":
            questions = f"Planning paused: {one_line((st.pause or {}).get('detail', ''))}\n\n{questions}"
        doc = DocField(questions, rows=12)
        answers = TextField("Answers", rows=6, placeholder="Answer by number. Anything you leave out, the planner decides with its recommendation.")
        repo = str(self.repo)

        def drafted(job: Job) -> str | None:
            return "◆ The plan is drafted: v reads it, a approves it" if job.code == 3 else None

        def send(text: str) -> Callable[[Form, App], None]:
            def go(form: Form, app: App) -> None:
                body = text or answers.value.strip() or "use your recommendations"
                app.close(form)
                app.launch("answers", ["say", payload("answers", body), "--repo", repo], then=drafted, starts=True)

            return go

        self.push(
            Form(
                "THE PLANNER ASKS",
                [doc, answers],
                [Button("Send answers", send("")), Button("Use its recommendations", send("use your recommendations")), Button("Cancel", lambda f, a: a.close(f))],
                width=100,
                accent=141,
                label_w=9,
                focus=1,
            )
        )

    def act_approve(self) -> None:
        if not self.guard(self.why_approve):
            return
        s, st, repo = self.snap, self.snap.state, str(self.repo)
        close = Button("Cancel", lambda m, a: a.close(m))
        if st.phase == "PLAN_REVIEW":
            self.approve_plan()
        elif s.waiting_changes:
            self.change_picker()
        elif st.contract and s.drift:
            self.push(
                Confirm(
                    "RE-APPROVE THE CONTRACT",
                    f"Changed since the last approval: {', '.join(s.drift)}. Re-approving records the current files as the "
                    "approved contract, with an owner entry in the ledger.",
                    [Button("Re-approve", lambda m, a: (a.close(m), a.launch("re-approve", ["approve", "--repo", repo]))), close],
                )
            )
        else:
            self.push(
                Confirm(
                    "ADOPT THE CONTRACT",
                    "Adopt this repo's PRD, CLAUDE.md and bridge.toml as the approved contract? It appends an owner decision "
                    "to the ledger. Nothing runs yet; start with r.",
                    [Button("Adopt", lambda m, a: (a.close(m), a.launch("adopt", ["approve", "--no-run", "--repo", repo]))), close],
                )
            )

    def approve_plan(self) -> None:
        repo = str(self.repo)
        summary = self.snap.plan_summary.strip() or "The planner wrote the contract files."
        body = summary + "\n\nApprove and build starts the loop now and runs until PROJECT COMPLETE or a pause."
        self.push(
            Confirm(
                "APPROVE THE PLAN",
                body,
                [
                    Button("Approve and build", lambda m, a: (a.close(m), a.launch("approve", ["approve", "--repo", repo], starts=True))),
                    Button("Approve only", lambda m, a: (a.close(m), a.launch("approve", ["approve", "--no-run", "--repo", repo]))),
                    Button("Read the plan", lambda m, a: (a.close(m), a.act_plan())),
                    Button("Cancel", lambda m, a: a.close(m)),
                ],
                accent=213,
                width=84,
            )
        )

    def change_picker(self) -> None:
        changes = self.snap.waiting_changes
        repo = str(self.repo)
        rows = [(f"{c.get('id')}  {c.get('title', '')}", "weakens" if c.get("weakens") else "material" if c.get("material") else "", Style(214)) for c in changes]

        def detail(i: int) -> str:
            c = changes[i]
            why = "; ".join(c.get("reasons") or [])
            affects = ", ".join(c.get("affects") or [])
            return f"{c.get('title')}\n\nReason: {c.get('reason', '')}\nAffects: {affects or '—'}\nWhy it waits for you: {why or '—'}\nDiff: {c.get('diff')}"

        def show_diff(i: int, app: App) -> None:
            path = self.repo / str(changes[i].get("diff", ""))
            app.push(Viewer([Tab(changes[i].get("id", "diff"), lambda: _read(path), path=None, kind="diff")], title="PLAN CHANGE"))

        def approve(i: int, app: App) -> None:
            app.close()
            app.launch(f"approve {changes[i].get('id')}", ["approve", str(changes[i].get("id")), "--repo", repo])

        def reject(i: int, app: App) -> None:
            reason = LineField("Reason", placeholder="Why — the planner and the supervisor are told")

            def go(form: Form, a: App) -> None:
                if not reason.value.strip():
                    form.error = "give a reason"
                    return
                a.close(form)
                a.close()
                a.launch(f"reject {changes[i].get('id')}", ["approve", "--reject", str(changes[i].get("id")), "--reason", reason.value.strip(), "--repo", repo])

            app.push(Form(f"REJECT {changes[i].get('id')}", [reason], [Button("Reject", go), Button("Cancel", lambda f, a: a.close(f))], accent=203))

        self.push(Picker("PLAN CHANGES WAITING", rows, detail, [("enter", "diff", show_diff), ("y", "approve", approve), ("x", "reject", reject)], accent=214))

    def act_plan(self) -> None:
        if not self.guard(self.why_config):
            return
        cfg = self.snap.cfg
        assert cfg is not None
        p = cfg.project
        tabs = []
        summary = self.watcher.sd.plan / "plan.md"
        if self.snap.state.phase == "PLAN_REVIEW" and summary.exists():
            tabs.append(Tab("Summary", lambda: _read(summary)))
        for title, path in (("PRD", p.prd), (p.rules[0].name, p.rules[0]), ("Ledger", p.decisions), ("Open items", p.open_items), ("bridge.toml", cfg.path)):
            tabs.append(Tab(title, lambda path=path: _read(path), path=path, kind="text" if path.suffix == ".toml" else "md"))
        actions = [("a", "approve", lambda v, a: (a.close(v), a.approve_plan()))] if self.snap.state.phase == "PLAN_REVIEW" else []
        self.push(Viewer(tabs, title="THE PLAN", actions=actions))

    def act_decide(self) -> None:
        if not self.guard(self.why_decide):
            return
        docs = sorted((self.repo / "docs").glob("OWNER_REVIEW*.md"), key=lambda p: p.stat().st_mtime)
        default = docs[-1].relative_to(self.repo).as_posix() if docs else "docs/OWNER_REVIEW.md"
        path = LineField("Decisions doc", default)
        auto = ToggleField("Let the planner answer its own questions with its recommendations")
        note = NoteField("Write D-items and a 'Done when' list. The planner turns them into plan changes recorded as OWNER DECISIONS, then the loop runs.")
        repo = str(self.repo)

        def target() -> Path:
            return (self.repo / path.value.strip()).resolve()

        def apply(form: Form, app: App) -> None:
            if not target().exists():
                form.error = f"{path.value} does not exist: write a template first"
                return
            argv = ["decide", path.value.strip(), "--repo", repo] + (["--auto-approve"] if auto.value else [])
            app.close(form)
            app.launch("decide", argv, starts=True)

        def template(form: Form, app: App) -> None:
            if target().exists():
                app.edit_file(target())
                return
            app.launch("template", ["review", "--template", path.value.strip(), "--repo", repo], then=lambda j: (app.edit_file(target()) if j.code == 0 else None) or None)

        self.push(
            Form(
                "OWNER DECISIONS",
                [path, auto, note],
                [Button("Apply decisions", apply), Button("Write / edit the doc", template), Button("Cancel", lambda f, a: a.close(f))],
                accent=214,
                label_w=14,
            )
        )

    def act_todo(self) -> None:
        if not self.guard(self.why_config):
            return
        items = []
        for section, label, colour in (("changes", "change", 214), ("decisions", "review", 214), ("blocked", "blocked", 203)):
            for item in self.snap.todo.get(section, []):
                items.append((section, item, label, colour))
        rows = [(f"{it.ident:<10} {one_line(it.title)}", label, Style(colour)) for _, it, label, colour in items]

        def detail(i: int) -> str:
            return todo_detail(items[i][1], self.repo)

        def show(i: int, app: App) -> None:
            item = items[i][1]
            if item.path is None:
                return
            app.push(Viewer([Tab(item.path.name, lambda: _read(item.path), path=item.path, line=item.line)], title="OWNER TO-DO"))

        self.push(Picker("OWNER TO-DO", rows, detail, [("enter", "open", show)], empty="✓ Nothing waits for you."))

    def act_report(self) -> None:
        if not self.guard(self.why_config):
            return
        report = self.watcher.sd.report

        def show(job: Job) -> str | None:
            if job.code == 0:
                self.push(Viewer([Tab("report.md", lambda: _read(report), path=report)], title="OWNER-REVIEW REPORT"))
                return "✓ report written"
            return None

        self.launch("report", ["report", "--repo", str(self.repo)], then=show)

    def act_logs(self) -> None:
        if not self.guard(self.why_config):
            return
        sd = self.watcher.sd

        def events() -> str:
            from agent_bridge.journal import read_events

            records = read_events(sd.events, tail_bytes=3_000_000)[-3000:] if sd.events.exists() else []
            return "\n".join(line for e in records if (line := render_event(e)))

        tabs = [
            Tab("Transcript", lambda: _read(sd.loop_log), kind="log"),
            Tab("Alerts", lambda: _read(sd.review_log), kind="log"),
            Tab("Console", lambda: _read(sd.console_log), kind="log"),
            Tab("Events", events, kind="log"),
        ]
        if sd.serve_log.exists():
            tabs.append(Tab("opencode server", lambda: _read(sd.serve_log), kind="log"))
        self.push(Viewer(tabs, title="LOGS", wrap_on=False, index=0))

    def act_check(self) -> None:
        if not self.guard(self.why_config):
            return

        def show(job: Job) -> str | None:
            self.push(Viewer([Tab("agent-bridge check", job.output, kind="log")], title="SETUP CHECK"))
            return "✓ setup check passed" if job.code == 0 else None

        self.launch("check", ["check", "--repo", str(self.repo)], then=show)

    def act_fresh(self, role: str) -> None:
        if not self.guard(self.why_fresh):
            return
        repo = str(self.repo)
        self.push(
            Confirm(
                f"FRESH {role.upper()}",
                f"The next {role} turn starts a new session. It re-reads the repo and the handoff; the old session is kept, not deleted.",
                [Button("Fresh session", lambda m, a: (a.close(m), a.launch(f"fresh {role}", ["pin", f"--new-{role}", "--repo", repo]))), Button("Cancel", lambda m, a: a.close(m))],
            )
        )

    def default_new_folder(self) -> Path:
        here = self.home_root
        if not (here / "bridge.toml").exists() and not self.snap.contract_files and here != Path.home():
            if (here / ".git").exists() or (here.is_dir() and not any(here.iterdir())):
                return here
        base = Path.home() / "src"
        return (base if base.is_dir() else here) / "new-project"

    def act_new(self) -> None:
        folder = LineField("Folder", tilde(self.default_new_folder()))
        idea = TextField("Idea", rows=6, placeholder="What should be built? A few sentences is enough; the planner asks about the rest.")
        preset, custom = self.engine_fields()
        auto = ToggleField("Auto-approve: the planner answers itself and the build starts without me (every choice is logged)")

        def resolved() -> Path:
            return Path(os.path.expanduser(folder.value.strip() or ".")).resolve()

        needs_git = lambda: not (resolved() / ".git").exists()  # noqa: E731
        author = LineField("Git author", _git_value("user.name"), when=needs_git)
        email = LineField("Git email", _git_value("user.email"), when=needs_git)
        note = NoteField(lambda: f"A new git repo is created in {tilde(resolved())}, with this author for the builder's commits." if needs_git() else "The repo already exists; its own git identity is used.", T.FAINT)

        def create(form: Form, app: App) -> None:
            path = resolved()
            if path in (Path.home(), Path("/")) or len(path.parts) < 3:
                form.error = "choose a project folder, not your home folder"
                return
            if path.exists() and not path.is_dir():
                form.error = f"{tilde(path)} is a file"
                return
            taken = [p for p in ("bridge.toml", "docs/PRD.md", "CLAUDE.md") if (path / p).exists()]
            if taken:
                form.error = f"{tilde(path)} already has {', '.join(taken)}: open it, or set it up with i"
                return
            if not idea.value.strip():
                form.error = "describe the idea first"
                return
            flags = self.role_flags(preset, custom)
            if isinstance(flags, str):
                form.error = flags
                return
            try:
                path.mkdir(parents=True, exist_ok=True)
                if not (path / ".git").exists():
                    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, timeout=30)
                    for key, value in (("user.name", author.value.strip()), ("user.email", email.value.strip())):
                        if value:
                            subprocess.run(["git", "-C", str(path), "config", key, value], check=True, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                form.error = f"could not prepare {tilde(path)}: {e}"
                return
            argv = ["new", payload("idea", idea.value.strip()), "--repo", str(path), *flags] + (["--auto-approve"] if auto.value else [])

            def asked(job: Job) -> str | None:
                return "◆ The planner has questions: press i to answer" if job.code == 3 else None

            app.close(form)
            app.launch("new project", argv, cwd=path, then=asked, starts=True)
            app.open(path)
            app.screen = "project"

        fields = [folder, idea, preset, *custom.values(), auto, author, email, note]
        self.push(Form("NEW PROJECT", fields, [Button("Create", create), Button("Cancel", lambda f, a: a.close(f))], width=96, accent=87, focus=1))

    def act_init(self) -> None:
        root = self.repo
        preset, custom = self.engine_fields()
        rules = ToggleField("Append the agent-bridge rules block to CLAUDE.md (labels, WAIT, the contract, commits)", True)
        legacy = ToggleField(
            "Also adopt the old bridge's state: its opencode sessions, and its unsent reply or last builder report. "
            "Leave off for a fresh start (docs/MIGRATION.md says which projects want it).",
            False,
            when=lambda: self.snap.legacy,
        )
        old_rules = NoteField(
            "This repo has an old tools/bridge.py. Its project rules for the supervisor live in that script's SYSTEM and "
            "AUTONOMOUS prompts: copy the project parts into docs/SUPERVISOR.md first, and init will use it.",
            T.WARN,
            when=lambda: (root / "tools" / "bridge.py").exists() and not (root / "docs" / "SUPERVISOR.md").exists(),
        )
        note = NoteField("Writes bridge.toml (never overwrites one) and adds .bridge/ to .gitignore. Then a setup check runs. No model calls.", T.FAINT)

        def go(form: Form, app: App) -> None:
            flags = self.role_flags(preset, custom)
            if isinstance(flags, str):
                form.error = flags
                return
            argv = ["init", "--repo", str(root), *flags] + (["--write-rules"] if rules.value else []) + (["--adopt-legacy"] if legacy.value and self.snap.legacy else [])

            def after(job: Job) -> str | None:
                if job.code != 0:
                    return None
                app.open(root)
                app.launch("check", ["check", "--repo", str(root)], then=self.after_check)
                return "✓ wrote bridge.toml; checking the setup"

            app.close(form)
            app.launch("init", argv, cwd=root, then=after)

        self.push(Form("SET UP THIS REPO", [preset, *custom.values(), rules, legacy, old_rules, note], [Button("Set up", go), Button("Cancel", lambda f, a: a.close(f))], width=96, accent=87))

    def why_phases(self) -> str | None:
        if self.why_config():
            return self.why_config()
        if self.snap.state.contract is None:
            return "approve a contract first"
        if self.snap.holder:
            return "stop the bridge first"
        if not any(status != "done" for _, status in self.snap.phases):
            return "no open Phase headings in the PRD"
        return self.why_busy()

    def act_phases(self) -> None:
        if not self.guard(self.why_phases):
            return
        open_phases = [(title, contract.phase_token(title)) for title, status in self.snap.phases if status != "done"]
        toggles = [ToggleField(title) for title, _ in open_phases]
        evidence = LineField("Evidence", placeholder="where it was verified: a commit, a WORKLOG entry, the README")
        note = NoteField("For phases finished before this project moved to agent-bridge. Each is recorded in the ledger as your word, not as verified by the supervisor.", T.FAINT)

        def go(form: Form, app: App) -> None:
            chosen = [display_token(token) for (_, token), t in zip(open_phases, toggles) if t.value and token]
            if not chosen:
                form.error = "tick at least one phase"
                return
            argv = ["approve", *[x for name in chosen for x in ("--done", name)], "--repo", str(self.repo)]
            if evidence.value.strip():
                argv[-2:-2] = ["--reason", evidence.value.strip()]
            app.close(form)
            app.launch("record phases", argv)

        self.push(Form("PHASES ALREADY COMPLETE", [note, *toggles, evidence], [Button("Record", go), Button("Cancel", lambda f, a: a.close(f))], width=96, accent=84))

    def after_check(self, job: Job) -> str | None:
        actions = []
        if self.snap.state.contract is None and self.snap.contract_files:
            actions.append(("a", "adopt the contract", lambda v, a: (a.close(v), a.act_approve())))
        self.push(Viewer([Tab("agent-bridge check", job.output, kind="log")], title="SETUP CHECK", actions=actions))
        return "✓ setup check passed: a adopts the contract" if job.code == 0 else None

    def act_add_folder(self) -> None:
        folder = LineField("Folder", "", placeholder="~/src/my-project")

        def go(form: Form, app: App) -> None:
            path = Path(os.path.expanduser(folder.value.strip())).resolve()
            if not path.is_dir():
                form.error = f"{tilde(path)} is not a folder"
                return
            app.close(form)
            app.open(path)
            if not app.snap.configured:
                app.toast("Not set up yet: press i to set it up, or n for a new project", "info")
            app.screen = "project"

        self.push(Form("OPEN A FOLDER", [folder], [Button("Open", go), Button("Cancel", lambda f, a: a.close(f))], width=80))

    def commands(self) -> list[Command]:
        return [
            Command("Run the loop", "r", lambda a: a.act_run(), self.why_run, "run start resume"),
            Command("Stop after this step", "s", lambda a: a.act_stop(), self.why_stop, "stop pause"),
            Command("Message both agents", "m", lambda a: a.act_message("both"), self.why_message, "say owner"),
            Command("Message the builder only", "", lambda a: a.act_message("builder"), self.why_message, "say owner"),
            Command("Message the supervisor only", "", lambda a: a.act_message("supervisor"), self.why_message, "say owner"),
            Command("Answer the planner", "i", lambda a: a.act_answer(), self.why_answer, "interview questions"),
            Command("Approve the plan, a plan change, or the contract", "a", lambda a: a.act_approve(), self.why_approve, "approve adopt reject"),
            Command("Read the plan files", "v", lambda a: a.act_plan(), self.why_config, "prd rules ledger view"),
            Command("Apply an owner decisions doc", "d", lambda a: a.act_decide(), self.why_decide, "decide review template"),
            Command("Owner to-do", "o", lambda a: a.act_todo(), self.why_config, "review blocked decisions"),
            Command("Owner-review report", "p", lambda a: a.act_report(), self.why_config, "report"),
            Command("Logs", "l", lambda a: a.act_logs(), self.why_config, "transcript console events alerts"),
            Command("Check the setup (no model calls)", "c", lambda a: a.act_check(), self.why_config, "check doctor"),
            Command("Record phases finished before agent-bridge", "", lambda a: a.act_phases(), self.why_phases, "phase done migration"),
            Command("Fresh builder session", "", lambda a: a.act_fresh("builder"), self.why_fresh, "pin rotate"),
            Command("Fresh supervisor session", "", lambda a: a.act_fresh("supervisor"), self.why_fresh, "pin rotate"),
            Command("Fresh planner session", "", lambda a: a.act_fresh("planner"), self.why_fresh, "pin rotate"),
            Command("New project", "n", lambda a: a.act_new(), lambda: None, "create plan idea"),
            Command("Set up this repo", "", lambda a: a.act_init(), lambda: None if self.snap.contract_files and not self.snap.configured else "only for a repo with a PRD and CLAUDE.md and no bridge.toml", "init adopt"),
            Command("Open a folder", "+", lambda a: a.act_add_folder(), lambda: None, "add project"),
            Command("All projects", "^A", lambda a: a.toggle_projects(), lambda: None, "list switch"),
            Command("Wrap long stream lines", "w", lambda a: setattr(a.view, "wrap", not a.view.wrap), lambda: None, "wrap"),
            Command("Help", "?", lambda a: a.act_help(), lambda: None, "keys"),
            Command("Quit the view (the bridge keeps running)", "q", lambda a: a.act_quit(), lambda: None, "exit close"),
        ]


# ----------------------------------------------------------------- curses


class Colors:
    def __init__(self) -> None:
        self.on = curses.has_colors()
        self.default_ok = False
        if self.on:
            curses.start_color()
            try:
                curses.use_default_colors()
                self.default_ok = True
            except curses.error:
                pass
        self.n = curses.COLORS if self.on else 0
        self.max_pairs = min(curses.COLOR_PAIRS, 32767) if self.on else 0
        self.pairs: dict[tuple[int, int], int] = {}
        self.next = 1
        self.cache: dict[Style, int] = {}

    def colour(self, c: int, fallback: int) -> int:
        if c < 0:
            return -1 if self.default_ok else fallback
        if self.n >= 256:
            return c
        return nearest_basic(c, 16 if self.n >= 16 else 8)

    def attr(self, s: Style) -> int:
        cached = self.cache.get(s)
        if cached is not None:
            return cached
        flags = 0
        if s.bold:
            flags |= curses.A_BOLD
        if s.dim:
            flags |= curses.A_DIM
        if s.underline:
            flags |= curses.A_UNDERLINE
        if s.reverse:
            flags |= curses.A_REVERSE
        if s.italic:
            flags |= getattr(curses, "A_ITALIC", 0)
        if self.on:
            fg = self.colour(s.fg, 7)
            bg = self.colour(-1 if s.bg == INHERIT else s.bg, 0)
            flags |= curses.color_pair(self.pair(fg, bg))
        self.cache[s] = flags
        return flags

    def pair(self, fg: int, bg: int) -> int:
        if (fg, bg) == (-1, -1):
            return 0
        found = self.pairs.get((fg, bg))
        if found is not None:
            return found
        if self.next >= self.max_pairs:
            return 0
        try:
            curses.init_pair(self.next, fg, bg)
        except curses.error:
            return 0
        self.pairs[(fg, bg)] = self.next
        self.next += 1
        return self.pairs[(fg, bg)]


class Screen:
    def __init__(self, win: Any) -> None:
        self.win = win
        self.colors = Colors()
        self.prev: list[Any] = []

    def invalidate(self) -> None:
        self.prev = []
        self.win.clear()

    def show(self, cv: Canvas) -> None:
        if len(self.prev) != cv.h:
            self.prev = [None] * cv.h
        for y in range(cv.h):
            runs = cv.runs(y)
            if self.prev[y] == runs:
                continue
            self.prev[y] = runs
            for x, text, style in runs:
                try:
                    self.win.addstr(y, x, text, self.colors.attr(style))
                except curses.error:
                    pass
        try:
            if cv.cursor:
                curses.curs_set(1)
                self.win.move(*cv.cursor)
            else:
                curses.curs_set(0)
        except curses.error:
            pass
        self.win.noutrefresh()
        curses.doupdate()


def _read_key(win: Any) -> str | int | None:
    try:
        return win.get_wch()
    except curses.error:
        return None


def _suspend(screen: Screen) -> None:
    curses.def_prog_mode()
    curses.endwin()
    os.kill(os.getpid(), signal.SIGTSTP)
    curses.reset_prog_mode()
    screen.invalidate()


def _run_editor(screen: Screen, app: App) -> None:
    assert app.editor is not None
    kind, target, then = app.editor
    app.editor = None
    try:
        command = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano") or ["nano"]
    except ValueError as e:
        app.toast(f"$EDITOR is not a command the shell could run ({e}); fix it, then try again", "bad", 10)
        return
    tmp = None
    if kind == "text":
        fd, name = tempfile.mkstemp(prefix="agent-bridge-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(target)
        tmp = Path(name)
        path = tmp
    else:
        path = Path(target)
    curses.def_prog_mode()
    curses.endwin()
    try:
        subprocess.run([*command, str(path)])
    except OSError as e:
        app.toast(f"cannot start the editor `{command[0]}`: {e}", "bad")
    finally:
        curses.reset_prog_mode()
        screen.invalidate()
    if tmp is not None:
        if then:
            then(tmp.read_text(encoding="utf-8").rstrip("\n"))
        tmp.unlink(missing_ok=True)
    elif then:
        then()


def _bursts(keys: list[str | int]) -> list[str | int | tuple[str, str]]:
    """Runs of three or more text characters read in one go (a paste) become one ("paste", text) item."""
    out: list[str | int | tuple[str, str]] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 3:
            out.append(("paste", "".join(run)))
        else:
            out.extend(run)
        run.clear()

    for k in keys:
        if isinstance(k, str) and (k.isprintable() or k in ("\t", "\n", "\r")) and len(k) == 1:
            run.append(k)
        else:
            flush()
            out.append(k)
    flush()
    return out


def loop(stdscr: Any, app: App) -> None:
    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen = Screen(stdscr)
    stdscr.timeout(40)
    drawn = 0.0
    while not app.quit:
        app.tick()
        if app.editor:
            _run_editor(screen, app)
        now = time.monotonic()
        if app.dirty or now - drawn >= (0.08 if app.animating() else 0.5):
            h, w = stdscr.getmaxyx()
            screen.show(app.render(h, w))
            drawn, app.dirty = now, False
        k = _read_key(stdscr)
        if k is None:
            continue
        keys = [k]
        stdscr.timeout(0)
        while len(keys) < 65536:
            more = _read_key(stdscr)
            if more is None:
                break
            keys.append(more)
        stdscr.timeout(40)
        for raw in _bursts(keys):
            if isinstance(raw, tuple):
                app.paste(raw[1])
                if app.editor:
                    _run_editor(screen, app)
                continue
            name = keyname(raw)
            if name == "resize":
                curses.update_lines_cols()
                screen.invalidate()
                app.dirty = True
            elif name == "ctrl-z":
                _suspend(screen)
            else:
                app.key(name)
            if app.editor:
                _run_editor(screen, app)
            if app.quit:
                break


def run_tui(folder: Path | None = None, *, show_all: bool = False) -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        # LANG names a locale this machine lacks; fall back rather than refuse to start.
        for name in ("C.UTF-8", "C"):
            try:
                locale.setlocale(locale.LC_ALL, name)
                break
            except locale.Error:
                continue
    os.environ.setdefault("ESCDELAY", "25")
    holder: dict[str, App] = {}

    def body(stdscr: Any) -> None:
        app = App(Path(folder or os.getcwd()), show_all=show_all, splash=os.environ.get("AGENT_BRIDGE_NO_SPLASH") != "1")
        holder["app"] = app
        loop(stdscr, app)

    curses.wrapper(body)
    app = holder.get("app")
    if app is not None:
        s = app.snap
        if s.holder:
            print(f"agent-bridge is still running {s.name} (pid {s.holder.get('pid')}). `agent-bridge` reopens this view; `agent-bridge stop` stops it.")
        busy = app.runner.busy()
        if busy:
            print("still running in the background: " + ", ".join(j.label for j in busy))
    return 0
