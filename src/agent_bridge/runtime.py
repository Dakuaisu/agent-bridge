"""Putting a run together: config, backends, preflight, the lock, caffeinate, the banner."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from agent_bridge import contract
from agent_bridge.backends.base import Backend, BackendError, Unsupported
from agent_bridge.backends.claude_code import ClaudeCodeBackend
from agent_bridge.backends.opencode import OpencodeBackend, version_warning
from agent_bridge.clock import RealClock, iso
from agent_bridge.config import CONFIG_NAME, ROLES, Config, load_config
from agent_bridge.engine import State, new_pending
from agent_bridge.journal import Journal
from agent_bridge.live import foreground_printer
from agent_bridge.planner import ContractEngine
from agent_bridge.repo import Repo
from agent_bridge.report import write_report
from agent_bridge.statedir import BridgeLock, StateDir, atomic_write_json, read_json


class UsageError(Exception):
    pass


def find_repo(path: Path | None) -> Path:
    start = Path(path or os.getcwd()).resolve()
    top = Repo(start).toplevel()
    if top is None:
        raise UsageError(f"{start} is not inside a git repository (run `git init` first, or pass --repo)")
    return top.resolve()


def config_path(repo: Path, explicit: Path | None) -> Path:
    return Path(explicit).resolve() if explicit else repo / CONFIG_NAME


def load_env_file(path: Path | None) -> dict[str, str]:
    """KEY=VALUE lines; values are never logged."""
    if path is None or not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            env[key] = value
    return env


BackendFactory = Callable[[Config, StateDir], dict[str, Backend]]


def make_backends(cfg: Config, sd: StateDir) -> dict[str, Backend]:
    env_extra = load_env_file(cfg.project.env_file)
    builder_on_opencode = cfg.role("builder").engine == "opencode"
    backends: dict[str, Backend] = {}
    for role in ROLES:
        rc = cfg.role(role)
        common = {"repo": cfg.project.repo, "project": cfg.project.name}
        if rc.engine == "claude-code":
            backends[role] = ClaudeCodeBackend(
                rc, billing_mode=cfg.billing_mode, git_push=cfg.git_push, env_extra=env_extra if role == "builder" else None, **common
            )
        else:
            # One server serves every opencode role, so the env file reaches it if the builder is on opencode.
            backends[role] = OpencodeBackend(
                rc,
                port=cfg.opencode.port or 0,
                accept=cfg.opencode.accept,
                serve_log=sd.serve_log,
                billing_mode=cfg.billing_mode,
                env_extra=env_extra if builder_on_opencode else None,
                **common,
            )
    return backends


_factory: BackendFactory = make_backends


def set_backend_factory(factory: BackendFactory) -> None:
    """Tests swap in FakeBackends here."""
    global _factory
    _factory = factory


def build_engine(cfg: Config, sd: StateDir, *, echo: bool = True, live: bool = True, confirm=None) -> ContractEngine:
    sd.ensure()
    clock = RealClock()
    journal = Journal(sd, clock, echo=echo)
    if live and echo:
        journal.add_listener(foreground_printer())
    engine = ContractEngine(cfg, sd, journal, _factory(cfg, sd), clock=clock, confirm=confirm)
    engine.on_complete = lambda eng: _report_on_complete(eng)
    return engine


def _report_on_complete(engine: ContractEngine) -> None:
    path = write_report(engine.cfg, engine.sd, engine.st, now=engine.now())
    engine.j.console(f"Owner-review report: {path} (latest copy: {engine.sd.report})")


def preflight(engine: ContractEngine, roles: tuple[str, ...] = ROLES) -> tuple[list[str], list[str]]:
    """(fatal problems, warnings); no model calls."""
    fatal: list[str] = []
    warnings: list[str] = []
    for role in roles:
        backend = engine.backends[role]
        try:
            backend.version_check()
            health = backend.health_check()
        except Unsupported as e:
            fatal.append(f"{role}: {e}")
            continue
        except BackendError as e:
            fatal.append(f"{role}: {e}")
            continue
        if not health.ok:
            fatal.append(f"{role} ({backend.engine}): {health.detail}")
        warnings += [f"{role}: {w}" for w in health.warnings]
    if not engine.cfg.uses_opencode():
        w = version_warning("opencode", engine.cfg.opencode.accept)
        if w:
            warnings.append(w)
    _, email = engine.repo.local_identity()
    if not email:
        warnings.append("this repo has no local git user.email; the builder's commits will use your global identity")
    p = engine.cfg.project
    if p.supervisor_rules and not p.supervisor_rules.exists():
        warnings.append(f"project.supervisor_rules names {p.supervisor_rules.relative_to(p.repo)}, which does not exist; the supervisor runs without project rules")
    elif not p.supervisor_rules and (p.repo / "tools" / "bridge.py").exists():
        warnings.append(
            "this repo has an old tools/bridge.py but no project.supervisor_rules: the old bridge's project rules for the "
            "supervisor are not loaded (copy them into docs/SUPERVISOR.md; docs/MIGRATION.md section 1)"
        )
    return fatal, warnings


def banner(engine: ContractEngine) -> list[str]:
    lines = [f"agent-bridge: {engine.cfg.project.name} ({engine.cfg.project.repo})"]
    for role in ROLES:
        backend = engine.backends[role]
        cap = backend.capabilities()
        lines.append(f"  {role:<10} {backend.engine} {backend.cfg.engine_model()}; read-only: {cap.read_only}")
        if cap.live_attach and role == "builder":
            lines.append(f"  {'':<10} watch it: {cap.live_attach}")
    lines.append(f"  logs: {engine.sd.loop_log}; stop: agent-bridge stop")
    return lines


def start_caffeinate(enabled: bool) -> subprocess.Popen[bytes] | None:
    if not enabled or sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    return subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_locked(engine: ContractEngine, argv: list[str], body: Callable[[], int]) -> int:
    lock = BridgeLock(engine.sd.lock)
    lock.acquire({"started": iso(engine.now()), "argv": argv})
    try:
        engine.sd.clear_stop()
        engine.j.launch_marker(argv)
        start_caffeinate(engine.cfg.safety.caffeinate)
        return body()
    finally:
        lock.release()


def spawn_background(repo: Path, argv: list[str], sd: StateDir) -> int:
    sd.ensure()
    log = sd.console_log.open("a", encoding="utf-8")
    env = dict(os.environ, AGENT_BRIDGE_BACKGROUND="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_bridge", *argv],
        cwd=repo,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    log.close()
    return proc.pid


def in_background() -> bool:
    return os.environ.get("AGENT_BRIDGE_BACKGROUND") == "1"


def import_legacy(sd: StateDir, cfg: Config, state: State) -> list[str]:
    """Adopt an old tools/bridge.py .bridge/ folder: pinned sessions and the pending review or reply."""
    notes = []
    now = iso(RealClock().now())
    old = sd.root
    registry = read_json(sd.sessions, default=[]) or []
    for role, name in (("builder", "session"), ("supervisor", "supervisor_session")):
        path = old / name
        if path.exists() and path.read_text().strip():
            sid = path.read_text().strip()
            if cfg.role(role).engine != "opencode":
                notes.append(f"{name}: {sid} is an opencode session but [{role}] uses {cfg.role(role).engine}; not adopted")
                continue
            state.sessions[role] = {"engine": "opencode", "id": sid, "started": now, "closed": False, "adopted": True, "needs_role": role == "supervisor"}
            registry.append({"role": role, "engine": "opencode", "id": sid, "title": "(adopted from the old bridge)", "directory": str(cfg.project.repo), "created": now, "retired": None, "adopted": True})
            notes.append(f"adopted the old {role} session {sid}")
    atomic_write_json(sd.sessions, registry)
    unsent = old / "unsent_reply.md"
    last = old / "builder_last.md"
    has_unsent = unsent.exists() and bool(unsent.read_text().strip())
    has_last = last.exists() and bool(last.read_text().strip())
    # The old bridge ran past an unsent reply if a later builder report exists; delivering it would be stale.
    stale = has_unsent and has_last and last.stat().st_mtime > unsent.stat().st_mtime
    if stale:
        notes.append(
            f"unsent_reply.md ({_stamp(unsent)}) is older than builder_last.md ({_stamp(last)}): "
            "the old bridge ran past it, so it is not delivered"
        )
    if has_unsent and not stale:
        state.pending = new_pending(supervisor=unsent.read_text().strip(), supervisor_note="(saved by the old bridge when it stopped)")
        state.phase = "BUILDER_TURN"
        notes.append("the old bridge's unsent supervisor reply is the next builder message")
    elif has_last:
        report = sd.turns / "legacy-builder-report.md"
        sd.turns.mkdir(parents=True, exist_ok=True)
        report.write_text(last.read_text())
        state.review = {"report": str(report), "exchange": state.exchange, "nudges": [], "notes": ["This report was carried over from the old bridge."]}
        state.phase = "SUPERVISOR_TURN"
        notes.append("the old builder_last.md is the next report for the supervisor to review")
    return notes


def _stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def rules_block_diff(cfg: Config) -> tuple[str, str]:
    rules = cfg.project.rules[0]
    before = rules.read_text(encoding="utf-8") if rules.exists() else ""
    return before, contract.apply_block(before, cfg)
