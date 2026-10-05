from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_bridge.clock import FakeClock


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the owner's global git config, hooks and identity out of every test."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-10-06T12:00:00+05:30")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-06T12:00:00+05:30")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "Test Owner")
    git(r, "config", "user.email", "owner@example.invalid")
    git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("test repo\n")
    git(r, "add", "README.md")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
