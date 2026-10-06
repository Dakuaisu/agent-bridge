from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent_bridge.backends.base import AuthFailed, Cancelled, NeverCancel, SessionLimit, Timeout, TransientError, Unsupported
from agent_bridge.backends.claude_code import ClaudeCodeBackend, child_env, neutral_dir
from agent_bridge.config import load_config

from fakebin import calls, claude_error, claude_turn, install, scenario
from harness import BASE_TOML


@pytest.fixture
def setup(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-system-wide-key")
    monkeypatch.setenv("CLAUDECODE", "1")
    (repo / "bridge.toml").write_text(BASE_TOML + "[supervisor]\nvariant = 'xhigh'\n")
    cfg = load_config(repo / "bridge.toml")
    exe = install(tmp_path / "bin", "claude")

    def make(role: str, **kw) -> ClaudeCodeBackend:
        return ClaudeCodeBackend(cfg.role(role), repo=repo, project="demo", binary=str(exe), **kw)

    return make, tmp_path


def send(backend, message: str = "[bridge] hello", timeout: float = 20, cancel=None):
    events, sessions = [], []
    reply = backend.send(message, timeout=timeout, on_event=events.append, cancel=cancel or NeverCancel(), on_session=sessions.append)
    return reply, events, sessions


def test_supervisor_command_is_read_only_enforced_and_neutral(setup) -> None:
    make, tmp = setup
    record = scenario(tmp, [claude_turn("VERDICT: ok\nSCOPE: none\nREPLY:\ngo", tools=[{"name": "Read", "input": {"file_path": "/r/docs/PRD.md"}}]), claude_turn("second")])
    sup = make("supervisor")
    sup.system_prompt = "ROLE: supervisor"
    reply, events, sessions = send(sup, "[bridge] review this")
    first = calls(record)[0]
    args = first["args"]
    assert args[:6] == ["-p", "--output-format", "stream-json", "--verbose", "--model", "claude-fable-5-1"]
    assert args[args.index("--effort") + 1] == "xhigh"
    assert args[args.index("--session-id") + 1] == sessions[0]
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob" and args[args.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--add-dir") + 1] == str(sup.repo)
    assert args[args.index("--append-system-prompt") + 1] == "ROLE: supervisor"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert "--permission-mode" not in args
    assert first["cwd"] == str(neutral_dir("demo", sup.repo, "supervisor").resolve())
    assert sup.workdir == neutral_dir("demo", sup.repo, "supervisor") and make("builder").workdir == sup.repo
    assert first["stdin"] == "[bridge] review this"
    assert first["env"]["ANTHROPIC_API_KEY"] is False and first["env"]["CLAUDECODE"] is False
    assert reply.served_models == {"claude-fable-5-1"} and reply.context_tokens == 115
    assert [t.name for t in reply.tool_calls] == ["Read"] and reply.tool_calls[0].summary == "/r/docs/PRD.md"
    assert any(e.kind == "tool" for e in events) and any(e.kind == "text" for e in events)
    assert sup.capabilities().read_only == "enforced: --tools Read,Grep,Glob"
    send(sup)
    second = calls(record)[1]["args"]
    assert second[second.index("--resume") + 1] == sessions[0] and "--session-id" not in second


def test_builder_command(setup) -> None:
    make, tmp = setup
    record = scenario(tmp, [claude_turn("built it", model="claude-opus-5-5", tools=[{"name": "Bash", "input": {"command": "pytest -q"}}])])
    builder = make("builder")
    reply, _, _ = send(builder)
    first = calls(record)[0]
    args = first["args"]
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert args[args.index("--disallowedTools") + 1] == "AskUserQuestion"
    assert json.loads(args[args.index("--settings") + 1]) == {"permissions": {"deny": ["Bash(git push:*)", "Bash(git push)"]}}
    assert "--tools" not in args and "--effort" not in args
    assert first["cwd"] == str(builder.repo.resolve())
    assert reply.text == "built it" and reply.tool_calls[0].summary == "pytest -q"


def test_api_key_mode_keeps_the_key_and_push_allowed_drops_the_deny(setup) -> None:
    make, tmp = setup
    record = scenario(tmp, [claude_turn("ok", api_key_source="ANTHROPIC_API_KEY")])
    builder = make("builder", billing_mode="api-key", git_push="allowed")
    send(builder)
    first = calls(record)[0]
    assert first["env"]["ANTHROPIC_API_KEY"] is True and "--settings" not in first["args"]


def test_subscription_billing_refuses_an_api_key_session(setup) -> None:
    make, tmp = setup
    scenario(tmp, [claude_turn("ok", api_key_source="ANTHROPIC_API_KEY")])
    with pytest.raises(Unsupported, match="would bill that key"):
        send(make("builder"))


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Failed to authenticate: OAuth session expired and could not be refreshed", AuthFailed),
        ("You've hit your session limit · resets 9:40pm (Asia/Calcutta)", SessionLimit),
        ("API Error: 500 internal", TransientError),
    ],
)
def test_error_results_are_classified(setup, message: str, error: type) -> None:
    make, tmp = setup
    scenario(tmp, [claude_error(message)])
    with pytest.raises(error) as info:
        send(make("supervisor"))
    if error is SessionLimit:
        from agent_bridge.clock import IST

        assert info.value.reset_at is not None
        assert info.value.reset_at.astimezone(IST).strftime("%H:%M") == "21:40"


def test_unknown_flag_is_unsupported(setup) -> None:
    make, tmp = setup
    scenario(tmp, [{"lines": [], "stderr": "error: unknown option '--permission-prompts'\n", "exit": 1}])
    with pytest.raises(Unsupported, match="rejected a flag"):
        send(make("supervisor"))


def test_session_id_in_use_retries_with_resume(setup) -> None:
    make, tmp = setup
    record = scenario(tmp, [{"lines": [], "stderr": "Error: Session ID abc is already in use.\n", "exit": 1}, claude_turn("resumed")])
    reply, _, _ = send(make("supervisor"))
    assert reply.text == "resumed"
    assert "--resume" in calls(record)[1]["args"]


def test_timeout_ends_the_process(setup) -> None:
    make, tmp = setup
    scenario(tmp, [{"lines": [{"type": "system", "subtype": "init", "session_id": "s"}], "sleep": 30}])
    started = time.monotonic()
    with pytest.raises(Timeout):
        send(make("builder"), timeout=1.5)
    assert time.monotonic() - started < 15


def test_cancel_ends_the_process(setup) -> None:
    make, tmp = setup
    scenario(tmp, [{"lines": [], "sleep": 30}])

    class Flag:
        def __init__(self) -> None:
            self.at = time.monotonic() + 1.0

        def is_set(self) -> bool:
            return time.monotonic() > self.at

    with pytest.raises(Cancelled):
        send(make("builder"), cancel=Flag())


def test_empty_reply_is_transient(setup) -> None:
    make, tmp = setup
    scenario(tmp, [{"lines": [{"type": "result", "subtype": "success", "is_error": False, "result": ""}]}])
    with pytest.raises(TransientError, match="no text"):
        send(make("builder"))


def test_health_and_version(setup) -> None:
    make, tmp = setup
    scenario(tmp, [], auth={"loggedIn": False, "authMethod": "none"}, version="2.1.287 (Claude Code)")
    backend = make("supervisor")
    assert backend.version_check() == "2.1.287 (Claude Code)"
    health = backend.health_check()
    assert not health.ok and "claude login" in health.detail
    scenario(tmp, [], auth={"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})
    assert backend.health_check().ok


def test_missing_binary_is_unsupported(setup) -> None:
    make, _ = setup
    backend = make("builder")
    backend.binary = "/nonexistent/claude"
    with pytest.raises(Unsupported, match="not found"):
        backend.version_check()


def test_child_env_strips_only_what_it_should(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "k")
    env = child_env("subscription", {"EXTRA": "1"})
    assert "ANTHROPIC_API_KEY" not in env and env["KAGGLE_API_TOKEN"] == "k" and env["EXTRA"] == "1"
    assert child_env("api-key")["ANTHROPIC_API_KEY"] == "x"
