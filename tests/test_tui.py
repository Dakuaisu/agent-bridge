from __future__ import annotations

import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import fcntl
import time
from pathlib import Path

import pytest
import tui_scenes as S

from agent_bridge import cli, registry
from agent_bridge.journal import Journal
from agent_bridge.clock import FakeClock
from agent_bridge.statedir import StateDir
from agent_bridge.tui.app import App, Colors
from agent_bridge.tui.canvas import Canvas
from agent_bridge.tui.jobs import Runner
from agent_bridge.tui.model import ProjectWatcher, next_step
from agent_bridge.tui.modals import Confirm, Form, Palette, Picker, Viewer
from agent_bridge.tui.text import clean, clip, width, wrap
from agent_bridge.tui.theme import INHERIT, Style
from agent_bridge.tui.widgets import LineEdit, TextArea, keyname


class FakeProc:
    def __init__(self, cmd: list[str], **kw: object) -> None:
        self.cmd = cmd
        self.kw = kw
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


def app_for(folder: Path) -> App:
    app = App(folder, splash=False, runner=Runner(popen=FakeProc))
    app.tick()
    return app


def last_argv(app: App) -> list[str]:
    return app.runner.jobs[-1].argv


def keys(app: App, *names: str) -> None:
    for name in names:
        app.key(name)


def type_text(app: App, text: str) -> None:
    for ch in text:
        app.key("enter" if ch == "\n" else ch)


def screen(app: App, h: int = 42, w: int = 150) -> str:
    return "\n".join(app.render(h, w).text())


# ----------------------------------------------------------------- text, canvas, widgets


def test_widths_clip_and_wrap_count_terminal_cells() -> None:
    assert width("ab") == 2 and width("漢字") == 4 and width("e\u0301") == 1
    assert clip("漢字漢字", 5) == "漢字…" and width(clip("漢字漢字", 5)) <= 5
    assert clean("\x1b[31mred\x1b[0m\tx\r\n") == "red    x\n"
    assert wrap("- one two three four five", 12) == ["- one two", "  three four", "  five"]
    assert all(width(line) <= 8 for line in wrap("supercalifragilistic word", 8))


def test_canvas_wide_characters_styles_and_inherited_background() -> None:
    cv = Canvas(2, 10, bg=233)
    cv.fill(0, 0, 1, 10, Style(-1, 235))
    cv.put(0, 0, "a漢b", Style(51))
    assert cv.text()[0] == "a漢b"
    runs = cv.runs(0)
    assert runs[0][2].bg == 235 and runs[0][2].fg == 51
    cv.put(0, 2, "x")
    assert cv.chars[0][1] == " " and cv.text()[0].startswith("a x")
    end = cv.put(1, 8, "漢字")
    assert cv.text()[1] == " " * 8 + "漢" and end == 10
    assert "<span" in cv.to_html()
    assert Style().bg == INHERIT


def test_keys_and_line_editing() -> None:
    assert keyname("\x01") == "ctrl-a" and keyname("\x1b") == "esc" and keyname("\x7f") == "backspace" and keyname("\n") == "enter"
    e = LineEdit("hello")
    for k in ("home", "right", "x", "end", "backspace"):
        e.key(k)
    assert e.text == "hxell"
    e.set("one two")
    e.key("ctrl-w")
    assert e.text == "one "
    view, cur = LineEdit("a" * 50).view(10)
    assert width(view) <= 10 and cur <= 9


def test_text_area_wraps_and_moves_by_screen_rows() -> None:
    a = TextArea()
    a.cols = 5
    for ch in "abcdefgh":
        a.key(ch)
    assert a.visual() == [(0, 0, 5), (0, 5, 8)]
    assert a.cursor() == (1, 3)
    assert a.key("up") and a.cursor() == (0, 3)
    assert not a.key("up")
    a.key("end")
    a.key("enter")
    a.key("z")
    assert a.text == "abcdefgh\nz"
    a.key("backspace")
    a.key("backspace")
    assert a.text == "abcdefgh"


# ----------------------------------------------------------------- the model


def test_watcher_reads_a_running_project(tmp_path: Path) -> None:
    repo, lock = S.running(tmp_path)
    try:
        snap = ProjectWatcher(repo).refresh()
        assert snap.configured and snap.status == "running" and snap.holder
        assert snap.active and snap.active.role == "builder" and snap.active.since is not None
        assert snap.roles["supervisor"].read_only.startswith("enforced") and snap.roles["builder"].read_only == "writes"
        assert snap.roles["supervisor"].context == 171_800
        assert [s for _, s in snap.phases] == ["done", "current", "todo", "todo"]
        assert [c["id"] for c in snap.waiting_changes] == ["PC-001"]
        assert len(snap.todo["decisions"]) == 1 and len(snap.todo["blocked"]) == 1
        assert any("COMMIT ATTRIBUTION" in t for _, t in snap.warnings)
        tools = [line for line in snap.lines if line.kind == "tool"]
        assert any(line.extra == "src/ledger_sync/match.py" for line in tools)
        assert "PC-001 waits for your approval" in next_step(snap)[0]
    finally:
        assert lock
        lock.release()
    snap = ProjectWatcher(repo).refresh()
    assert snap.status == "idle" and snap.active is None


def test_watcher_tails_new_events_and_survives_a_truncated_log(tmp_path: Path) -> None:
    repo, lock = S.running(tmp_path, hold_lock=False)
    w = ProjectWatcher(repo)
    before = len(w.refresh().lines)
    Journal(StateDir(repo), FakeClock(), echo=False).event("owner_message", text="hello from the test", to="both")
    snap = w.refresh()
    assert len(snap.lines) == before + 1 and snap.lines[-1].text == "hello from the test"
    StateDir(repo).events.write_text(json.dumps({"ts": "2026-10-06T12:00:00+05:30", "kind": "owner_message", "text": "fresh"}) + "\n")
    assert [line.text for line in w.refresh().lines] == ["fresh"]


def test_the_other_states(tmp_path: Path) -> None:
    snap = ProjectWatcher(S.interview(tmp_path)).refresh()
    assert snap.status == "answers" and "Where do habits live" in snap.questions
    assert "«i»" in next_step(snap)[0]
    snap = ProjectWatcher(S.plan_review(tmp_path)).refresh()
    assert snap.status == "review" and "one phase" in snap.plan_summary
    snap = ProjectWatcher(S.blank(tmp_path)).refresh()
    assert not snap.configured and snap.status == "new" and "«n»" in next_step(snap)[0]
    snap = ProjectWatcher(S.blank(tmp_path, with_contract=True)).refresh()
    assert snap.contract_files and "«i»" in next_step(snap)[0]


# ----------------------------------------------------------------- actions run the CLI


def test_stop_message_and_change_decisions_launch_the_right_commands(tmp_path: Path) -> None:
    repo, lock = S.running(tmp_path)
    try:
        app = app_for(repo)
        root = str(app.repo)
        app.key("r")
        assert not app.modals and "already running" in app.toast_items[-1][0]
        app.key("s")
        assert isinstance(app.modals[-1], Confirm)
        app.key("enter")
        assert last_argv(app) == ["stop", "--repo", root]
        app.key("m")
        type_text(app, "Use the PRD's 3-day window.")
        app.key("ctrl-s")
        argv = last_argv(app)
        assert argv[0] == "say" and argv[2:] == ["--to", "both", "--repo", root]
        assert Path(argv[1][1:]).read_text() == "Use the PRD's 3-day window."
        app.key("a")
        assert isinstance(app.modals[-1], Picker)
        app.key("y")
        assert last_argv(app) == ["approve", "PC-001", "--repo", root] and not app.modals
        app.key("a")
        app.key("x")
        type_text(app, "keep one window")
        app.key("ctrl-s")
        assert last_argv(app) == ["approve", "--reject", "PC-001", "--reason", "keep one window", "--repo", root]
        assert not app.modals
    finally:
        assert lock
        lock.release()


def test_run_form_builds_the_run_command(tmp_path: Path) -> None:
    repo, _ = S.running(tmp_path, hold_lock=False)
    app = app_for(repo)
    app.key("r")
    assert isinstance(app.modals[-1], Form)
    keys(app, "right", "tab")
    for _ in range(3):
        app.key("backspace")
    type_text(app, "2")
    keys(app, "tab")
    type_text(app, "Start with the boundary tests.")
    keys(app, "tab", " ", "ctrl-s")
    argv = last_argv(app)
    assert argv[:4] == ["run", "--repo", str(app.repo), "--loop"] and argv[4] == "2"
    assert argv[5] == "--kickoff" and Path(argv[6][1:]).read_text() == "Start with the boundary tests."
    assert argv[7:] == ["--new-supervisor"]
    app.tick()
    assert app.view.starting


def test_interview_answers_and_plan_approval(tmp_path: Path) -> None:
    app = app_for(S.interview(tmp_path))
    assert app.why_run() and "answers" in app.why_run()  # type: ignore[operator]
    app.key("i")
    form = app.modals[-1]
    assert isinstance(form, Form) and "Where do habits live" in screen(app)
    type_text(app, "1. SQLite\n2. no")
    app.key("ctrl-s")
    argv = last_argv(app)
    assert argv[0] == "say" and argv[2:] == ["--repo", str(app.repo)] and Path(argv[1][1:]).read_text() == "1. SQLite\n2. no"

    app = app_for(S.plan_review(tmp_path))
    app.key("v")
    viewer = app.modals[-1]
    assert isinstance(viewer, Viewer) and [t.title for t in viewer.tabs][:2] == ["Summary", "PRD"]
    app.key("a")
    assert isinstance(app.modals[-1], Confirm) and not any(isinstance(m, Viewer) for m in app.modals)
    app.key("enter")
    assert last_argv(app) == ["approve", "--repo", str(app.repo)]


def test_new_project_and_init(tmp_path: Path) -> None:
    app = app_for(S.blank(tmp_path))
    assert "New project" in screen(app)
    app.key("n")
    form = app.modals[-1]
    assert isinstance(form, Form)
    type_text(app, "A CLI that counts words.")
    app.key("shift-tab")
    for _ in range(200):
        app.key("backspace")
    type_text(app, str(Path.home()))
    app.key("ctrl-s")
    assert "home folder" in form.error
    for _ in range(200):
        app.key("backspace")
    target = tmp_path / "brand-new"
    type_text(app, str(target))
    keys(app, "tab", "tab", "right")
    app.key("ctrl-s")
    argv = last_argv(app)
    assert argv[0] == "new" and argv[2:4] == ["--repo", str(target.resolve())]
    assert "--planner" in argv and "claude-code:claude-haiku-4-5-20251001" in argv
    assert (target / ".git").exists() and app.snap.root == target.resolve()

    old = S.blank(tmp_path, with_contract=True)
    app = app_for(old)
    assert "Set up this repo" in screen(app)
    app.key("i")
    app.key("ctrl-s")
    assert last_argv(app) == ["init", "--repo", str(app.repo), "--write-rules"]


def test_finished_jobs_toast_and_chain(tmp_path: Path) -> None:
    repo, _ = S.running(tmp_path, hold_lock=False)
    app = app_for(repo)
    app.act_check()
    job = app.runner.jobs[-1]
    job.log.write_text("$ agent-bridge check\nconfig: ok\n")
    job.proc.code = 0
    app.tick()
    assert isinstance(app.modals[-1], Viewer) and app.toast_items[-1][0].startswith("✓ setup check passed")
    app.close()
    app.act_report()
    job = app.runner.jobs[-1]
    job.log.write_text("$ agent-bridge report\nboom\n")
    job.proc.code = 2
    app.tick()
    assert app.toast_items[-1][0] == "✗ report (exit 2): boom"


def test_projects_list_and_palette(tmp_path: Path) -> None:
    repo, _ = S.running(tmp_path, hold_lock=False)
    S.interview(tmp_path)
    app = app_for(repo)
    app.key("ctrl-a")
    assert app.screen == "projects"
    text = screen(app)
    assert "ALL PROJECTS" in text and "habit-cli" in text and "(here)" in text
    names = [r.name for r in app.rows]
    assert names.count("ledger-sync") == 1
    app.plist.index = names.index("habit-cli")
    app.key("enter")
    assert app.screen == "project" and app.snap.name == "habit-cli"
    app.key(":")
    pal = app.modals[-1]
    assert isinstance(pal, Palette)
    type_text(app, "answer")
    assert [c.label for c in pal.matches()] == ["Answer the planner"]
    app.key("esc")
    assert not app.modals


@pytest.mark.parametrize("size", [(42, 150), (30, 100), (24, 80), (16, 60), (10, 40)])
def test_every_screen_renders_at_every_size(tmp_path: Path, size: tuple[int, int]) -> None:
    h, w = size
    repo, lock = S.running(tmp_path)
    try:
        folders = [repo, S.interview(tmp_path), S.plan_review(tmp_path), S.blank(tmp_path)]
        for folder in folders:
            app = app_for(folder)
            for action in (None, "?", ":", "v", "o", "m", "i", "a"):
                app.modals.clear()
                if action:
                    app.key(action)
                cv = app.render(h, w)
                assert len(cv.text()) == h
            app.key("ctrl-a")
            app.render(h, w)
        if w >= 60:
            assert "STREAM" in screen(app_for(repo), h, w)
    finally:
        assert lock
        lock.release()


def test_colours_fall_back_without_256_colour_support(monkeypatch: pytest.MonkeyPatch) -> None:
    import curses

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    colors = Colors()
    assert colors.attr(Style(51, 233, bold=True)) == curses.A_BOLD


# ----------------------------------------------------------------- the entry point


def test_no_arguments_without_a_terminal_prints_help(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main([]) == 0
    assert "usage: agent-bridge" in capsys.readouterr().out
    assert cli.main(["ui"]) == 2


def test_registry_keeps_one_entry_per_resolved_repo(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    registry.remember(a, "a")
    registry.remember(tmp_path / "a" / ".." / "a", "a")
    registry.remember(tmp_path, "root")
    assert [p["name"] for p in registry.projects()] == ["root", "a"]
    registry.forget(a)
    assert [p["name"] for p in registry.projects()] == ["root"]


@pytest.mark.parametrize("args", [["ui", "--repo", "{repo}"], []], ids=["ui", "no-arguments"])
def test_the_real_curses_app_runs_in_a_pseudo_terminal(tmp_path: Path, args: list[str]) -> None:
    """Starts the UI in a pty, presses keys, and quits; no model call is possible from these keys."""
    repo, lock = S.running(tmp_path)
    try:
        env = dict(os.environ, TERM="xterm-256color", AGENT_BRIDGE_NO_SPLASH="1", LANG="en_US.UTF-8")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_bridge", *(a.format(repo=repo) for a in args)],
            cwd=repo,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        out = b""

        def pump(seconds: float) -> None:
            nonlocal out
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        out += os.read(master, 65536)
                    except OSError:
                        return

        pump(1.5)
        for key in (b"?", b"\x1b", b"\x01", b"\x1b", b":", b"\x1b", b"v", b"\t", b"\x1b", b"q"):
            os.write(master, key)
            pump(0.3)
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            raise
        pump(0.2)
        os.close(master)
        text = out.decode("utf-8", "replace")
        assert code == 0, text[-2000:]
        assert "Traceback" not in text
        assert "STREAM" in text and "PROJECTS" in text and "PLAN" in text
        assert "still running ledger-sync" in text
    finally:
        assert lock
        lock.release()
