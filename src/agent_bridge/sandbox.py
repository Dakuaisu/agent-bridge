"""Prevent instead of detect: on macOS the builder's whole process tree may write only inside the repo,
temp folders and the tools' own state (sandbox-exec). Reads, the network and running programs are untouched.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Tool state, not project files: Claude Code's sessions, config and installs, package and build caches, git
# signing, Docker's CLI config. A project needing more lists it in [safety] sandbox_writable.
HOME_WRITABLE = (
    ".claude",
    ".local/share/claude",
    ".local/state/claude",
    ".config/claude",
    ".cache",
    ".npm",
    ".yarn",
    ".cargo",
    ".gradle",
    ".m2",
    ".docker",
    ".gnupg",
    ".kaggle",
    ".ipython",
    ".jupyter",
    ".matplotlib",
    "Library/Caches",
    "Library/Logs",
)
HOME_PREFIXES = (".claude.json",)  # the config file and the backups and temp files written beside it


def available() -> bool:
    return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()


def applies(mode: str) -> bool:
    return mode == "on" or (mode == "auto" and available())


def writable_roots(repo: Path, extra: tuple[str, ...] = ()) -> list[str]:
    home = Path(os.path.realpath(Path.home()))
    roots = {os.path.realpath(repo), "/private/tmp", "/private/var/folders", "/dev"}
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.add(os.path.realpath(tmpdir))
    roots.update(str(home / rel) for rel in HOME_WRITABLE)
    roots.update(os.path.realpath(os.path.expanduser(p)) for p in extra)
    return sorted(roots)


def _quote(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def profile(repo: Path, extra: tuple[str, ...] = ()) -> str:
    home = os.path.realpath(Path.home())
    allowed = [f'      (subpath "{_quote(root)}")' for root in writable_roots(repo, extra)]
    allowed += [f'      (regex #"^{_quote(re.escape(home + "/" + name))}")' for name in HOME_PREFIXES]
    return "\n".join(["(version 1)", "(allow default)", "(deny file-write*", "  (require-not", "    (require-any", *allowed, "    )))"]) + "\n"


def wrap(cmd: list[str], repo: Path, extra: tuple[str, ...] = ()) -> list[str]:
    return [SANDBOX_EXEC, "-p", profile(repo, extra), *cmd]


def describe(repo: Path) -> str:
    return f"sandboxed: writes only inside {repo}, temp folders and tool caches (sandbox-exec)"
