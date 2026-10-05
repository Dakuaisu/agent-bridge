"""Builds an engine on FakeBackends inside a temporary repo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_bridge.backends.fake import FakeBackend
from agent_bridge.clock import FakeClock
from agent_bridge.config import load_config
from agent_bridge.engine import Engine
from agent_bridge.journal import Journal, read_events
from agent_bridge.statedir import StateDir

BASE_TOML = "version = 1\n[project]\nname = 'demo'\n"


def make_engine(
    repo: Path,
    clock: FakeClock,
    *,
    builder: Any,
    supervisor: Any,
    planner: Any = None,
    toml: str = "",
    engine_cls: type[Engine] = Engine,
    supervisor_read_only: str | None = None,
    **kw: Any,
) -> Engine:
    path = repo / "bridge.toml"
    if not path.exists() or toml:
        path.write_text(BASE_TOML + toml)
    cfg = load_config(path)
    sd = StateDir(repo)
    sd.ensure()
    journal = Journal(sd, clock, echo=False)
    sup_kw = {"read_only": supervisor_read_only} if supervisor_read_only else {}
    backends = {
        "builder": FakeBackend(cfg.role("builder"), repo=repo, project="demo", clock=clock, script=builder),
        "supervisor": FakeBackend(cfg.role("supervisor"), repo=repo, project="demo", clock=clock, script=supervisor, **sup_kw),
    }
    if planner is not None:
        backends["planner"] = FakeBackend(cfg.role("planner"), repo=repo, project="demo", clock=clock, script=planner)
    return engine_cls(cfg, sd, journal, backends, clock=clock, **kw)


def review_log(repo: Path) -> str:
    path = repo / ".bridge" / "review.log"
    return path.read_text() if path.exists() else ""


def loop_log(repo: Path) -> str:
    return (repo / ".bridge" / "loop.log").read_text()


def events(repo: Path, kind: str | None = None) -> list[dict[str, Any]]:
    records = read_events(repo / ".bridge" / "events.jsonl")
    return [r for r in records if kind is None or r["kind"] == kind]


def ok(reply: str, scope: str = "R-1", verdict: str = "sound") -> str:
    return f"VERDICT: {verdict}\nSCOPE: {scope}\nREPLY:\n{reply}"


DONE = "PROJECT COMPLETE\nEverything verified.\nVERDICT: done\nREPLY:\nNothing further."
