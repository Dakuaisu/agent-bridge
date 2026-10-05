from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.backends.base import AuthFailed, NeverCancel, QuestionAsked, SessionLimit, Timeout, Unsupported
from agent_bridge.backends.opencode import OpencodeBackend, OpencodeDB, version_ok
from agent_bridge.config import load_config

from fakebin import calls, install, scenario
from harness import BASE_TOML

SES = "ses_abc123"


class FakeAPI:
    def __init__(self) -> None:
        self.health: Any = {"healthy": True, "version": "1.18.30"}
        self.questions: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self.posts: list[str] = []
        self.queries: list[str] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, data: Any, code: int = 200) -> None:
                body = json.dumps(data).encode() if not isinstance(data, bytes) else data
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                api.queries.append(query)
                if path == "/global/health":
                    return self._send(api.health if api.health is not None else b"<html>not opencode</html>")
                if path == "/question":
                    return self._send(api.questions)
                if path == "/session/status":
                    return self._send(api.status)
                self._send({}, 404)

            def do_POST(self) -> None:
                path = self.path.partition("?")[0]
                api.posts.append(path)
                if path.endswith("/abort"):
                    api.status.pop(path.split("/")[2], None)
                if path.startswith("/question/") and path.endswith("/reject"):
                    api.questions = [q for q in api.questions if q["id"] != path.split("/")[2]]
                self._send(True)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def api():
    server = FakeAPI()
    yield server
    server.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def make(repo: Path, tmp_path: Path):
    exe = install(tmp_path / "bin", "opencode")

    def build(role: str = "builder", port: int = 0, variant: str | None = None, db: OpencodeDB | None = None) -> OpencodeBackend:
        extra = f"[{role}]\nengine = 'opencode'\n" + (f"variant = '{variant}'\n" if variant else "") + f"[opencode]\nport = {port or 4999}\n"
        (repo / "bridge.toml").write_text(BASE_TOML + extra)
        cfg = load_config(repo / "bridge.toml")
        return OpencodeBackend(
            cfg.role(role), repo=repo, project="demo", port=port or 4999, serve_log=repo / ".bridge/serve.log", binary=str(exe), db=db or OpencodeDB(tmp_path / "none.db"), monitor_every=0.3
        )

    return build


def ev(kind: str, **part: Any) -> dict[str, Any]:
    return {"type": kind, "timestamp": 1, "sessionID": SES, "part": part}


def turn(text: str = "done", *, tools: bool = False, sleep: float = 0) -> dict[str, Any]:
    lines: list[Any] = [ev("step_start")]
    if sleep:
        lines.append({"sleep": sleep})
    if tools:
        lines.append(ev("tool_use", tool="bash", state={"status": "completed", "input": {"command": "pytest -q"}}))
        lines += [ev("step_finish", tokens={"input": 1, "cache": {"read": 2, "write": 3}}), ev("step_start")]
    lines += [ev("text", text=text), ev("step_finish", tokens={"input": 10, "output": 5, "cache": {"read": 1000, "write": 20}})]
    return {"lines": lines}


def send(backend, message: str = "[bridge] hello", timeout: float = 20, cancel=None):
    events, sessions = [], []
    reply = backend.send(message, timeout=timeout, on_event=events.append, cancel=cancel or NeverCancel(), on_session=sessions.append)
    return reply, events, sessions


def test_version_gate(make, tmp_path: Path) -> None:
    backend = make()
    scenario(tmp_path, [], version="1.18.30")
    assert backend.version_check() == "1.18.30"
    scenario(tmp_path, [], version="2.0.1")
    with pytest.raises(Unsupported, match="not supported; agent-bridge needs 1.18.x"):
        backend.version_check()
    assert version_ok("1.18.30", "1.18") and not version_ok("1.180.1", "1.18") and not version_ok("2.0.0", "1.18")


def test_turn_command_events_and_session(make, api: FakeAPI, tmp_path: Path) -> None:
    record = scenario(tmp_path, [turn("I ran the tests.", tools=True), turn("second")])
    backend = make("builder", port=api.port, variant="high")
    reply, events, sessions = send(backend, "[bridge] go")
    args = calls(record)[0]["args"]
    assert args[:2] == ["run", "--attach"] and args[2] == f"http://127.0.0.1:{api.port}"
    assert args[args.index("--dir") + 1] == str(backend.repo)
    assert "--auto" in args and args[args.index("--format") + 1] == "json"
    assert args[args.index("-m") + 1] == "anthropic/claude-opus-5-5" and args[args.index("--variant") + 1] == "high"
    assert args[args.index("--title") + 1].startswith("demo builder") and "--session" not in args
    assert args[-1] == "[bridge] go"
    assert calls(record)[0]["env"]["ANTHROPIC_API_KEY"] is False
    assert sessions == [SES] and reply.session_id == SES
    assert reply.text == "I ran the tests." and reply.steps == ["I ran the tests."]
    assert reply.tool_calls[0].name == "bash" and reply.tool_calls[0].summary == "pytest -q"
    assert reply.context_tokens == 1030
    send(backend)
    args2 = calls(record)[1]["args"]
    assert args2[args2.index("--session") + 1] == SES and "--title" not in args2
    assert all(f"directory={str(backend.repo).replace('/', '%2F')}" in q for q in api.queries)


def test_question_is_rejected_and_the_turn_aborted(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [turn(sleep=20)])
    api.questions = [{"id": "que_1", "sessionID": SES, "questions": [{"question": "Which deps?", "options": [{"label": "a"}, {"label": "b"}]}]}]
    backend = make("builder", port=api.port)
    started = time.monotonic()
    with pytest.raises(QuestionAsked) as info:
        send(backend)
    assert info.value.questions == ["Which deps? [a, b]"]
    assert "/question/que_1/reject" in api.posts and f"/session/{SES}/abort" in api.posts
    assert time.monotonic() - started < 15


def test_long_retry_becomes_a_session_limit(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [turn(sleep=20)])
    nxt = int((time.time() + 2 * 3600) * 1000)
    api.status = {SES: {"type": "retry", "attempt": 3, "message": "Claude pool exhausted — next account free in 2h", "next": nxt}}
    backend = make("supervisor", port=api.port)
    with pytest.raises(SessionLimit) as info:
        send(backend)
    assert abs(info.value.reset_at.timestamp() - nxt / 1000) < 2
    assert f"/session/{SES}/abort" in api.posts


def test_short_retry_is_left_to_opencode(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [turn("finished", sleep=1.2)])
    api.status = {SES: {"type": "retry", "attempt": 1, "message": "overloaded", "next": int((time.time() + 5) * 1000)}}
    reply, _, _ = send(make("builder", port=api.port))
    assert reply.text == "finished"


def test_timeout_aborts_on_the_server(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [turn(sleep=30)])
    with pytest.raises(Timeout):
        send(make("builder", port=api.port), timeout=1.5)
    assert f"/session/{SES}/abort" in api.posts


def test_busy_session_is_aborted_before_sending(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [turn("ok")])
    backend = make("builder", port=api.port)
    backend.resume(SES)
    api.status = {SES: {"type": "busy"}}
    _, events, _ = send(backend)
    assert api.posts[0] == f"/session/{SES}/abort"
    assert any("still busy; aborted it" in e.text for e in events)


def test_unrecognized_flag_is_unsupported(make, api: FakeAPI, tmp_path: Path) -> None:
    scenario(tmp_path, [{"lines": [], "stderr": "ERRORS\n  Unrecognized flag: --attach in command opencode run\n", "exit": 1}])
    with pytest.raises(Unsupported, match="still 1.18"):
        send(make("builder", port=api.port))


def test_error_event_is_classified(make, api: FakeAPI, tmp_path: Path) -> None:
    error = {"type": "error", "sessionID": SES, "error": {"name": "UnknownError", "data": {"message": "Failed to authenticate: OAuth session expired"}}}
    scenario(tmp_path, [{"lines": [ev("step_start"), error]}])
    with pytest.raises(AuthFailed):
        send(make("builder", port=api.port))


def test_capabilities_say_instruction_only_for_read_only_roles(make, api: FakeAPI) -> None:
    sup = make("supervisor", port=api.port)
    assert sup.capabilities().read_only == "read-only by instruction only"
    assert sup.capabilities().live_attach.startswith(f"opencode attach http://127.0.0.1:{api.port} --dir")


def test_server_identity_checks(make, api: FakeAPI) -> None:
    backend = make("builder", port=api.port)
    assert backend.server.ensure() is None
    api.health = {"healthy": True, "version": "2.0.1"}
    with pytest.raises(Unsupported, match="server 2.0.1"):
        backend.server.ensure()
    api.health = None
    with pytest.raises(Unsupported, match="not by a healthy opencode server"):
        backend.server.ensure()


def test_server_is_started_detached_when_nothing_listens(make, tmp_path: Path) -> None:
    scenario(tmp_path, [], version="1.18.30")
    port = free_port()
    backend = make("builder", port=port)
    pid = backend.server.ensure()
    try:
        assert pid and backend.server.is_up()
        assert os.getpgid(pid) != os.getpgid(0)
        assert f"agent-bridge started opencode serve on port {port}" in (backend.repo / ".bridge/serve.log").read_text()
        assert backend.server.ensure() is None
    finally:
        if pid:
            os.killpg(os.getpgid(pid), signal.SIGTERM)


def test_db_is_read_only_and_answers_lookups(tmp_path: Path) -> None:
    path = tmp_path / "opencode.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?, ?, ?)", (SES, "demo builder", "/repo"))
    for i, (role, model, t) in enumerate([("assistant", "claude-opus-5-5", 100), ("assistant", "claude-sonnet-5", 200), ("user", None, 300), ("assistant", "old", 50)]):
        conn.execute("INSERT INTO message VALUES (?, ?, ?, ?)", (f"m{i}", SES, t, json.dumps({"role": role, "modelID": model})))
    conn.commit()
    conn.close()
    db = OpencodeDB(path)
    assert db.session(SES) == {"id": SES, "title": "demo builder", "directory": "/repo"}
    assert db.session("ses_missing") is None and db.session("x'; DROP TABLE session; --") is None
    assert db.served_models(SES, 100) == {"claude-opus-5-5", "claude-sonnet-5"}
    ro = db._connect()
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM session")
    ro.close()
    assert OpencodeDB(tmp_path / "missing.db").served_models(SES, 0) == set()
