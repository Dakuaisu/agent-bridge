"""The projects agent-bridge has opened on this machine, most recent first (the Ctrl-A list)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_bridge.clock import RealClock, iso
from agent_bridge.statedir import atomic_write_json, read_json, state_home

LIMIT = 200


def registry_path() -> Path:
    return state_home() / "projects.json"


def projects() -> list[dict[str, Any]]:
    try:
        data = read_json(registry_path(), default=None)
    except (OSError, ValueError):
        return []
    items = data.get("projects") if isinstance(data, dict) else None
    return [p for p in items if isinstance(p, dict) and isinstance(p.get("repo"), str)] if isinstance(items, list) else []


def remember(repo: Path, name: str) -> None:
    """Never raises: the list is a convenience, and a failed write must not stop a command."""
    try:
        key = str(Path(repo).resolve())
        rest = [p for p in projects() if p["repo"] != key]
        entry = {"repo": key, "name": name, "seen": iso(RealClock().now())}
        atomic_write_json(registry_path(), {"projects": [entry, *rest][:LIMIT]})
    except OSError:
        pass


def forget(repo: Path | str) -> None:
    try:
        key = str(Path(repo).resolve())
        atomic_write_json(registry_path(), {"projects": [p for p in projects() if p["repo"] not in (key, str(repo))]})
    except OSError:
        pass
