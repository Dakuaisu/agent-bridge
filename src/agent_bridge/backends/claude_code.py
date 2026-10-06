"""Claude Code via `claude -p --output-format stream-json` (docs/DESIGN.md 4.3)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_bridge.backends.base import (
    READ_ONLY_ENFORCED,
    AuthFailed,
    BillingFailed,
    Backend,
    CancelToken,
    Cancelled,
    Capabilities,
    Event,
    Health,
    RateLimited,
    Reply,
    SessionLimit,
    Timeout,
    ToolCall,
    TransientError,
    Unsupported,
)
from agent_bridge import sandbox
from agent_bridge.backends.proc import kill_group, run_streaming
from agent_bridge.config import RoleConfig
from agent_bridge.limits import classify, parse_reset

READ_TOOLS = "Read,Grep,Glob"
STRIP_ALWAYS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
STRIP_FOR_SUBSCRIPTION = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# These send Claude Code to another endpoint or provider, which bills elsewhere.
STRIP_CLAUDE_ROUTING = (
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
)
_UNKNOWN_OPTION = re.compile(r"unknown option|unrecognized|invalid option|error: option", re.IGNORECASE)


def project_id(name: str, repo: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    return f"{slug}-{hashlib.sha1(str(repo).encode()).hexdigest()[:8]}"


def neutral_dir(name: str, repo: Path, role: str) -> Path:
    """A per-role cwd outside the repo, so the builder's CLAUDE.md never loads into a read-only role."""
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = base / "agent-bridge" / project_id(name, repo) / role
    path.mkdir(parents=True, exist_ok=True)
    return path


def child_env(billing_mode: str, extra: dict[str, str] | None = None, *, engine: str = "claude-code") -> dict[str, str]:
    """The agent's environment. Keys are stripped after the env_file is merged, so it cannot bring an API key back."""
    env = dict(os.environ)
    env.update(extra or {})
    for key in STRIP_ALWAYS:
        env.pop(key, None)
    if billing_mode == "subscription":
        for key in STRIP_FOR_SUBSCRIPTION + (STRIP_CLAUDE_ROUTING if engine == "claude-code" else ()):
            env.pop(key, None)
    return env


def tool_summary(name: str, data: dict[str, Any]) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "description"):
        if isinstance(data.get(key), str):
            return data[key][:500]
    return json.dumps(data)[:300]


class ClaudeCodeBackend(Backend):
    engine = "claude-code"

    def __init__(
        self,
        role_cfg: RoleConfig,
        *,
        repo: Path,
        project: str,
        billing_mode: str = "subscription",
        git_push: str = "never",
        binary: str = "claude",
        env_extra: dict[str, str] | None = None,
        sandboxed: bool = False,
        sandbox_writable: tuple[str, ...] = (),
    ) -> None:
        super().__init__(role_cfg, repo=repo, project=project)
        self.sandboxed = sandboxed and not role_cfg.read_only
        self.sandbox_writable = sandbox_writable
        self.billing_mode = billing_mode
        self.git_push = git_push
        self.binary = binary
        self.env_extra = env_extra or {}
        self._started = False
        self._proc: subprocess.Popen[str] | None = None

    # -- setup and health

    @property
    def cwd(self) -> Path:
        return self.repo if self.role == "builder" else neutral_dir(self.project, self.repo, self.role)

    @property
    def workdir(self) -> Path:
        return self.cwd

    def _exe(self) -> str:
        exe = shutil.which(self.binary) if os.sep not in self.binary else self.binary
        if not exe or not Path(exe).exists():
            raise Unsupported(f"`{self.binary}` not found on PATH; install Claude Code and log in with `claude login`")
        return exe

    def version_check(self) -> str:
        out = subprocess.run([self._exe(), "--version"], capture_output=True, text=True, timeout=60)
        version = out.stdout.strip() or out.stderr.strip()
        if out.returncode != 0 or not version:
            raise Unsupported(f"`claude --version` failed: {out.stderr.strip()[:300]}")
        return version

    def health_check(self) -> Health:
        out = subprocess.run(
            [self._exe(), "auth", "status"], capture_output=True, text=True, timeout=60, env=child_env(self.billing_mode, self.env_extra)
        )
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            return Health(False, f"`claude auth status` gave no JSON: {(out.stdout or out.stderr).strip()[:200]}")
        if not data.get("loggedIn"):
            return Health(False, "Claude Code is not logged in: run `claude login`")
        detail = f"logged in ({data.get('authMethod')}, {data.get('subscriptionType') or 'no subscription'})"
        warnings = []
        if self.billing_mode == "subscription" and data.get("authMethod") not in (None, "claude.ai"):
            warnings.append(f"billing.mode is subscription but Claude Code reports authMethod={data.get('authMethod')}")
        return Health(True, detail + " (a pre-check only: a dead login shows up on the first call)", tuple(warnings))

    def capabilities(self) -> Capabilities:
        if self.cfg.read_only:
            return Capabilities(read_only=READ_ONLY_ENFORCED, question_guard="no question tool available; --permission-prompts none")
        guard = sandbox.describe(self.repo) if self.sandboxed else "not sandboxed: writes outside the repo are found after the turn"
        return Capabilities(read_only="n/a (the builder writes)", question_guard="AskUserQuestion disallowed; --permission-prompts none", write_guard=guard)

    def session_directory(self, session_id: str) -> str | None:
        for path in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
            try:
                with path.open(encoding="utf-8") as f:
                    for _, line in zip(range(20), f):
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            return str(cwd)
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def resume(self, session_id: str) -> None:
        super().resume(session_id)
        self._started = True

    def abort(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            kill_group(proc, grace=5.0)

    def _process_started(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        if self.on_process:
            self.on_process(proc)

    def _process_ended(self) -> None:
        self._proc = None
        if self.on_process:
            self.on_process(None)

    def start_session(self, title: str) -> None:
        super().start_session(title)
        self._started = False

    # -- the turn

    def command(self, *, session_args: list[str]) -> list[str]:
        cmd = [self._exe(), "-p", "--output-format", "stream-json", "--verbose", "--model", self.cfg.engine_model()]
        if self.cfg.variant:
            cmd += ["--effort", self.cfg.variant]
        cmd += session_args
        if self.system_prompt:
            cmd += ["--append-system-prompt", self.system_prompt]
        cmd += ["--permission-prompts", "none", "-n", f"{self.project} {self.role}"]
        if self.cfg.read_only:
            cmd += ["--tools", READ_TOOLS, "--allowedTools", READ_TOOLS, "--add-dir", str(self.repo)]
        else:
            cmd += ["--permission-mode", "bypassPermissions", "--disallowedTools", "AskUserQuestion"]
            if self.git_push == "never":
                cmd += ["--settings", json.dumps({"permissions": {"deny": ["Bash(git push:*)", "Bash(git push)"]}})]
        return cmd

    def send(
        self,
        message: str,
        *,
        timeout: float,
        on_event: Callable[[Event], None],
        cancel: CancelToken,
        on_session: Callable[[str], None],
        raw_path: Path | None = None,
    ) -> Reply:
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
            self._started = False
        on_session(self._session_id)
        reply = self._turn(message, timeout=timeout, on_event=on_event, cancel=cancel, raw_path=raw_path, fresh=not self._started)
        self._started = True
        return reply

    def _turn(
        self,
        message: str,
        *,
        timeout: float,
        on_event: Callable[[Event], None],
        cancel: CancelToken,
        raw_path: Path | None,
        fresh: bool,
    ) -> Reply:
        session_args = ["--session-id", self._session_id] if fresh else ["--resume", self._session_id]
        cmd = self.command(session_args=session_args)
        if self.sandboxed:
            cmd = sandbox.wrap(cmd, self.repo, self.sandbox_writable)
        # The billing check runs on the first event: a turn on the wrong account is ended before it does any work.
        parser = _StreamParser(on_event, on_init=self._check_billing)
        raw = raw_path.open("a", encoding="utf-8") if raw_path else None
        started = time.monotonic()

        def on_line(line: str) -> None:
            if raw:
                raw.write(line + "\n")
            parser.feed(line)

        try:
            result = run_streaming(
                cmd,
                cwd=str(self.cwd),
                env=child_env(self.billing_mode, self.env_extra),
                stdin_text=message,
                timeout=timeout,
                cancel=cancel,
                on_line=on_line,
                on_stall=lambda s: on_event(Event("status", f"no output for {int(s // 60)} minutes; still running")),
                on_start=self._process_started,
            )
        finally:
            self._process_ended()
            if raw:
                raw.close()
        if result.cancelled:
            raise Cancelled("stop --now: the claude process was ended")
        if result.timed_out:
            raise Timeout(f"no reply within {int(timeout)}s; the claude process was ended", partial=parser.all_text())
        stderr = result.stderr.strip()
        if fresh and "already in use" in stderr.lower() and parser.result is None:
            return self._turn(message, timeout=timeout, on_event=on_event, cancel=cancel, raw_path=raw_path, fresh=False)
        self._check_billing(parser.init)
        if parser.result is None:
            text = stderr or "\n".join(result.lines[-5:])
            if _UNKNOWN_OPTION.search(text):
                raise Unsupported(f"this Claude Code CLI rejected a flag: {text[:400]}")
            raise _classified(text or f"claude exited {result.returncode} with no result", datetime.now().astimezone())
        res = parser.result
        if res.get("is_error"):
            raise _classified(str(res.get("result") or res.get("error") or res), datetime.now().astimezone())
        text = parser.all_text() or str(res.get("result") or "")
        if not text.strip():
            raise TransientError("claude returned no text")
        return Reply(
            text=text,
            session_id=self._session_id,
            served_models=parser.models,
            tool_calls=parser.tools,
            context_tokens=parser.context_tokens,
            duration_s=time.monotonic() - started,
            raw_path=raw_path,
            steps=parser.steps,
            cost_usd=parser.cost_usd,
            usage=parser.usage,
        )

    def _check_billing(self, init: dict[str, Any] | None) -> None:
        if not init or "apiKeySource" not in init:
            return
        source = init.get("apiKeySource")
        if self.billing_mode == "subscription" and source not in (None, "none"):
            raise Unsupported(f"billing.mode is subscription but Claude Code used an API key ({source}); it would bill that key")
        if self.billing_mode == "api-key" and source in (None, "none"):
            raise Unsupported("billing.mode is api-key but Claude Code used the subscription login")


def _classified(text: str, now: datetime) -> Exception:
    kind = classify(text)
    if kind == "auth":
        return AuthFailed(f"{text[:300]}. Run `claude login`.")
    if kind == "billing":
        return BillingFailed(text[:300])
    if kind == "session_limit":
        return SessionLimit(text[:300], reset_at=parse_reset(text, now))
    if kind == "rate_limited":
        return RateLimited(text[:300], reset_at=parse_reset(text, now))
    return TransientError(text[:500])


class _StreamParser:
    def __init__(self, on_event: Callable[[Event], None], on_init: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.on_event = on_event
        self.on_init = on_init
        self.cost_usd: float | None = None
        self.usage: dict[str, int] = {}
        self.init: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.steps: list[str] = []
        self.models: set[str] = set()
        self.tools: list[ToolCall] = []
        self.context_tokens: int | None = None

    def all_text(self) -> str:
        return "\n".join(s for s in self.steps if s.strip())

    def feed(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            self.init = data
            if self.on_init:
                self.on_init(data)
        elif kind == "assistant":
            # A subagent's messages carry the Task call's id: its tool calls still count for the safety audits,
            # but its text and model are not the role's own.
            subagent = bool(data.get("parent_tool_use_id"))
            msg = data.get("message") or {}
            if msg.get("model") and not subagent:
                self.models.add(msg["model"])
            usage = msg.get("usage") or {}
            if usage and not subagent:
                self.context_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            texts = []
            for block in msg.get("content") or []:
                if block.get("type") == "text" and block.get("text") and not subagent:
                    texts.append(block["text"])
                    self.on_event(Event("text", block["text"]))
                elif block.get("type") == "tool_use":
                    call = ToolCall(block.get("name", "?"), tool_summary(block.get("name", ""), block.get("input") or {}))
                    self.tools.append(call)
                    self.on_event(Event("tool", call.summary, tool=("subagent " if subagent else "") + call.name))
            if texts:
                self.steps.append("\n".join(texts))
        elif kind == "result":
            self.result = data
            if isinstance(data.get("total_cost_usd"), (int, float)):
                self.cost_usd = float(data["total_cost_usd"])
            usage = data.get("usage") or {}
            self.usage = {k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") if usage.get(k)}
        elif kind == "system":
            subtype = str(data.get("subtype", ""))
            if any(word in subtype for word in ("retry", "limit", "error", "compact")):
                self.on_event(Event("status", subtype, data={k: v for k, v in data.items() if k != "type"}))
