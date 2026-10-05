"""opencode 1.18.x via `opencode run --attach` to a per-project `opencode serve` (docs/DESIGN.md 4.2).

The server routes and the `retry` status shape come from the 1.18.30 binary; anything unexpected in
a response is treated as "unknown", never as success.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_bridge.backends.base import (
    READ_ONLY_BY_INSTRUCTION,
    AuthFailed,
    Backend,
    CancelToken,
    Cancelled,
    Capabilities,
    Event,
    Health,
    QuestionAsked,
    RateLimited,
    Reply,
    SessionLimit,
    Timeout,
    ToolCall,
    TransientError,
    Unsupported,
)
from agent_bridge.backends.claude_code import child_env
from agent_bridge.backends.proc import MonitorStop, run_streaming
from agent_bridge.config import RoleConfig
from agent_bridge.limits import classify, parse_reset

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.jsonc"
SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")
LONG_RETRY = 300
_UNRECOGNIZED = re.compile(r"unrecognized flag|unknown argument|unknown option", re.IGNORECASE)
V2_HELP = (
    "opencode {version} is not supported; agent-bridge needs 1.18.x. On 2.x, `run` has no --attach or --dir, "
    "the oh-my-openagent plugin cannot load, and Anthropic bills third-party apps from extra usage. Pin it: "
    "`npm install -g opencode-ai@1.18.30`, and keep \"autoupdate\": false in ~/.config/opencode/opencode.jsonc."
)


def installed_version(binary: str = "opencode") -> str | None:
    exe = shutil.which(binary) if os.sep not in binary else binary
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def version_ok(version: str, accept: str) -> bool:
    return version == accept or version.startswith(accept + ".")


def autoupdate_disabled(path: Path = CONFIG_PATH) -> bool | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return bool(re.search(r'"autoupdate"\s*:\s*false', text))


def version_warning(binary: str, accept: str) -> str | None:
    """For init/run/check when no role uses opencode: warn early, never fail."""
    version = installed_version(binary)
    if version and not version_ok(version, accept):
        return f"opencode {version} is installed; agent-bridge accepts {accept}.x, so a role switched to opencode would be refused"
    return None


class OpencodeDB:
    """~/.local/share/opencode/opencode.db, opened read-only. Never written."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
            conn.execute("PRAGMA query_only = 1")
            return conn
        except sqlite3.Error:
            return None

    def session(self, session_id: str) -> dict[str, Any] | None:
        if not SESSION_ID.match(session_id):
            return None
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT id, title, directory FROM session WHERE id = ?", (session_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return {"id": row[0], "title": row[1], "directory": row[2]} if row else None

    def served_models(self, session_id: str, since_ms: int) -> set[str]:
        if not SESSION_ID.match(session_id):
            return set()
        conn = self._connect()
        if conn is None:
            return set()
        try:
            rows = conn.execute(
                "SELECT DISTINCT json_extract(data, '$.modelID') FROM message "
                "WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant' AND time_created >= ?",
                (session_id, since_ms),
            ).fetchall()
        except sqlite3.Error:
            return set()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}


class OpencodeServer:
    def __init__(self, *, binary: str, port: int, repo: Path, serve_log: Path, env: dict[str, str], accept: str) -> None:
        self.binary = binary
        self.port = port
        self.repo = repo
        self.serve_log = serve_log
        self.env = env
        self.accept = accept
        self.url = f"http://127.0.0.1:{port}"

    def is_up(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                return True
        except OSError:
            return False

    def api(self, method: str, path: str, *, timeout: float = 10) -> Any:
        query = urllib.parse.urlencode({"directory": str(self.repo)})
        request = urllib.request.Request(f"{self.url}{path}?{query}", data=b"" if method == "POST" else None, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        return json.loads(body) if body else None

    def health(self) -> dict[str, Any] | None:
        try:
            data = self.api("GET", "/global/health", timeout=5)
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return data if isinstance(data, dict) else None

    def check_identity(self) -> str:
        data = self.health()
        if data is None or not data.get("healthy", data.get("ok", False)):
            raise Unsupported(f"port {self.port} is in use, but not by a healthy opencode server; choose another [opencode] port")
        version = str(data.get("version") or "")
        if version and not version_ok(version, self.accept):
            raise Unsupported(V2_HELP.format(version=f"server {version}"))
        return version or "unknown version"

    def ensure(self) -> int | None:
        """Start `opencode serve` if nothing is listening; returns the pid it started, if any."""
        if self.is_up():
            self.check_identity()
            return None
        self.serve_log.parent.mkdir(parents=True, exist_ok=True)
        with self.serve_log.open("a", encoding="utf-8") as log:
            log.write(f"\n=== agent-bridge started opencode serve on port {self.port} at {datetime.now().astimezone().isoformat(timespec='seconds')} ===\n")
        log = self.serve_log.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
            cwd=self.repo,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.close()
        for _ in range(60):
            if self.is_up() and self.health() is not None:
                self.check_identity()
                return proc.pid
            time.sleep(0.5)
        raise TransientError(f"opencode serve did not come up on port {self.port}; see {self.serve_log}")


class OpencodeBackend(Backend):
    engine = "opencode"

    def __init__(
        self,
        role_cfg: RoleConfig,
        *,
        repo: Path,
        project: str,
        port: int,
        accept: str = "1.18",
        serve_log: Path,
        billing_mode: str = "subscription",
        binary: str = "opencode",
        env_extra: dict[str, str] | None = None,
        db: OpencodeDB | None = None,
        monitor_every: float = 15.0,
    ) -> None:
        super().__init__(role_cfg, repo=repo, project=project)
        self.binary = binary
        self.accept = accept
        self.db = db or OpencodeDB()
        self.monitor_every = monitor_every
        exe = shutil.which(binary) if os.sep not in binary else binary
        self._exe = exe or binary
        self.server = OpencodeServer(
            binary=self._exe, port=port, repo=repo, serve_log=serve_log, env=child_env(billing_mode, env_extra), accept=accept
        )
        self.started_server_pid: int | None = None

    def version_check(self) -> str:
        if not Path(self._exe).exists():
            raise Unsupported(f"`{self.binary}` not found; install opencode 1.18.30 (`npm install -g opencode-ai@1.18.30`)")
        version = installed_version(self._exe)
        if not version:
            raise Unsupported("`opencode --version` printed nothing")
        if not version_ok(version, self.accept):
            raise Unsupported(V2_HELP.format(version=version))
        return version

    def health_check(self) -> Health:
        warnings = []
        if autoupdate_disabled() is False:
            warnings.append('~/.config/opencode/opencode.jsonc does not set "autoupdate": false; opencode may upgrade itself to 2.x')
        if self.server.is_up():
            version = self.server.check_identity()
            return Health(True, f"opencode server on {self.server.url} ({version})", tuple(warnings))
        return Health(True, f"no opencode server on {self.server.url} yet; `run` starts one", tuple(warnings))

    def capabilities(self) -> Capabilities:
        attach = f"opencode attach {self.server.url} --dir {self.repo}" + (f" --session {self._session_id}" if self._session_id else "")
        read_only = READ_ONLY_BY_INSTRUCTION if self.cfg.read_only else "n/a (the builder writes)"
        return Capabilities(read_only=read_only, question_guard="polls the server; rejects and aborts any question", live_attach=attach)

    def session_directory(self, session_id: str) -> str | None:
        info = self.db.session(session_id)
        return info["directory"] if info else None

    # -- server calls that must never raise during a turn

    def _safe(self, method: str, path: str) -> Any:
        try:
            return self.server.api(method, path)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def abort(self) -> None:
        if self._session_id:
            self._safe("POST", f"/session/{self._session_id}/abort")

    def _status(self) -> dict[str, Any] | None:
        data = self._safe("GET", "/session/status")
        if isinstance(data, dict) and self._session_id:
            status = data.get(self._session_id)
            return status if isinstance(status, dict) else None
        return None

    def _questions(self) -> list[dict[str, Any]]:
        data = self._safe("GET", "/question")
        if not isinstance(data, list):
            return []
        return [q for q in data if isinstance(q, dict) and (not self._session_id or q.get("sessionID") == self._session_id)]

    def command(self, message: str) -> list[str]:
        cmd = [self._exe, "run", "--attach", self.server.url, "--dir", str(self.repo), "--format", "json", "--auto", "-m", self.cfg.engine_model()]
        if self.cfg.variant:
            cmd += ["--variant", self.cfg.variant]
        cmd += ["--session", self._session_id] if self._session_id else ["--title", self._title]
        cmd.append(message)
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
        if message.lstrip().startswith("-"):
            raise TransientError("refusing a message that starts with '-': opencode would read it as a flag")
        pid = self.server.ensure()
        if pid:
            self.started_server_pid = pid
            on_event(Event("status", f"started opencode serve (pid {pid}) on {self.server.url}"))
        if self._session_id:
            status = self._status()
            if status and status.get("type") in ("busy", "retry"):
                self.abort()
                on_event(Event("status", f"session {self._session_id} was still {status.get('type')}; aborted it before sending"))
        parser = _EventParser(on_event)
        started_ms = int(time.time() * 1000)
        serve_offset = self.server.serve_log.stat().st_size if self.server.serve_log.exists() else 0
        raw = raw_path.open("a", encoding="utf-8") if raw_path else None

        def on_line(line: str) -> None:
            if raw:
                raw.write(line + "\n")
            parser.feed(line)
            if parser.session_id and parser.session_id != self._session_id:
                self._session_id = parser.session_id
                on_session(parser.session_id)

        def monitor() -> None:
            questions = self._questions()
            if questions:
                for q in questions:
                    if q.get("id"):
                        self._safe("POST", f"/question/{q['id']}/reject")
                self.abort()
                raise MonitorStop(QuestionAsked(_question_texts(questions)))
            status = self._status()
            if status and status.get("type") == "retry":
                limit = self._limit_from_retry(status, serve_offset)
                if limit is not None:
                    self.abort()
                    raise MonitorStop(limit)

        try:
            result = run_streaming(
                self.command(message),
                cwd=str(self.repo),
                env=self.server.env,
                stdin_text=None,
                timeout=timeout,
                cancel=cancel,
                on_line=on_line,
                monitor=monitor,
                monitor_every=self.monitor_every,
                on_stall=lambda s: on_event(Event("status", f"no output for {int(s // 60)} minutes; still running")),
            )
        except MonitorStop as stop:
            raise stop.error from None
        finally:
            if raw:
                raw.close()
        if result.cancelled:
            self.abort()
            raise Cancelled("stop --now: the opencode turn was aborted on the server")
        if result.timed_out:
            self.abort()
            raise Timeout(f"no reply within {int(timeout)}s; the turn was aborted on the server", partial=parser.all_text())
        if parser.errors:
            raise _classified(parser.errors[0], datetime.now().astimezone())
        if result.returncode not in (0, None):
            text = result.stderr.strip() or "\n".join(result.lines[-5:])
            if _UNRECOGNIZED.search(text):
                raise Unsupported(f"opencode rejected a flag; is it still 1.18.x? {text[:400]}")
            raise _classified(text or f"opencode exited {result.returncode}", datetime.now().astimezone())
        text = parser.all_text()
        if not text.strip():
            raise TransientError("opencode returned no text")
        served = self.db.served_models(self._session_id or "", started_ms) or parser.models
        return Reply(
            text=text,
            session_id=self._session_id or "",
            served_models=served,
            tool_calls=parser.tools,
            context_tokens=parser.context_tokens,
            duration_s=(int(time.time() * 1000) - started_ms) / 1000,
            raw_path=raw_path,
            steps=parser.steps(),
        )

    def _limit_from_retry(self, status: dict[str, Any], serve_offset: int) -> Exception | None:
        message = str(status.get("message") or "")
        reset = None
        nxt = status.get("next")
        if isinstance(nxt, (int, float)) and nxt > 0:
            reset = datetime.fromtimestamp(nxt / 1000 if nxt > 10**11 else nxt, tz=timezone.utc).astimezone()
        new_log = ""
        try:
            with self.server.serve_log.open(encoding="utf-8", errors="replace") as f:
                f.seek(serve_offset)
                new_log = f.read()[-20000:]
        except OSError:
            pass
        now = datetime.now().astimezone()
        if reset is None:
            reset = parse_reset(message, now) or parse_reset(new_log, now)
        far = reset is not None and (reset - now).total_seconds() > LONG_RETRY
        kind = classify(message) if message else classify(new_log)
        if kind == "auth":
            return AuthFailed(f"{message or new_log[-300:]}. Run `claude login` (or re-enroll the pool account).")
        if kind == "session_limit" or (far and "exhausted" in new_log.lower()):
            return SessionLimit(message or "usage limit (from serve.log)", reset_at=reset)
        if far:
            return RateLimited(message or "long provider retry", reset_at=reset)
        return None


def _question_texts(found: list[dict[str, Any]]) -> list[str]:
    out = []
    for q in found:
        for item in q.get("questions") or []:
            options = ", ".join(str(o.get("label", "")) for o in item.get("options") or [] if isinstance(o, dict))
            text = str(item.get("question", ""))
            out.append(f"{text} [{options}]" if options else text)
    return out or ["(question text unavailable)"]


def _classified(text: str, now: datetime) -> Exception:
    kind = classify(text)
    if kind == "auth":
        return AuthFailed(f"{text[:300]}. Run `claude login` (or re-enroll the pool account).")
    if kind == "session_limit":
        return SessionLimit(text[:300], reset_at=parse_reset(text, now))
    if kind == "rate_limited":
        return RateLimited(text[:300], reset_at=parse_reset(text, now))
    if "rejected permission" in text.lower():
        return TransientError(f"{text[:300]} (a permission prompt was rejected; the bridge always passes --auto)")
    return TransientError(text[:500])


class _EventParser:
    def __init__(self, on_event: Callable[[Event], None]) -> None:
        self.on_event = on_event
        self.session_id: str | None = None
        self._steps: list[list[str]] = [[]]
        self.tools: list[ToolCall] = []
        self.errors: list[str] = []
        self.models: set[str] = set()
        self.context_tokens: int | None = None

    def steps(self) -> list[str]:
        return [t for t in ("".join(s).strip() for s in self._steps) if t]

    def all_text(self) -> str:
        return "\n".join(self.steps())

    def feed(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if data.get("sessionID") and not self.session_id:
            self.session_id = data["sessionID"]
        kind = data.get("type")
        part = data.get("part") or {}
        if kind == "step_start":
            self._steps.append([])
        elif kind == "text":
            text = part.get("text", "")
            self._steps[-1].append(text)
            if text.strip():
                self.on_event(Event("text", text))
        elif kind == "tool_use":
            state = part.get("state") or {}
            data_in = state.get("input") or {}
            summary = next((str(data_in[k]) for k in ("command", "filePath", "path", "pattern", "url") if k in data_in), json.dumps(data_in)[:300])
            call = ToolCall(str(part.get("tool", "?")), summary[:500], ok=state.get("status") == "completed")
            self.tools.append(call)
            self.on_event(Event("tool", call.summary, tool=call.name))
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            if tokens:
                self.context_tokens = int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
        elif kind == "error":
            err = data.get("error") or {}
            message = (err.get("data") or {}).get("message") if isinstance(err, dict) else None
            self.errors.append(str(message or (err.get("name") if isinstance(err, dict) else err) or data))
            self.on_event(Event("error", self.errors[-1]))
