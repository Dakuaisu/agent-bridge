"""Git, read-only: snapshots for the tripwire, new commits for the trailer and author checks,
remote refs for push detection. The bridge never commits, pushes or edits git config."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Tool state, never project content: the bridge's own folder and the omo opencode plugin's (L22 smoke run).
EXCLUDES = (":(exclude).bridge", ":(exclude).omo")
ATTRIBUTION = re.compile(r"(?im)^\s*co-authored-by:|generated (?:with|by) (?:claude|opencode|an? ai)|noreply@anthropic\.com")


@dataclass(frozen=True)
class Snapshot:
    head: str | None
    status: dict[str, str] = field(default_factory=dict)
    diffs: dict[str, str] = field(default_factory=dict)
    remotes: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        blob = repr((self.head, sorted(self.status.items()), sorted(self.diffs.items())))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author_name: str
    author_email: str
    subject: str
    body: str

    @property
    def short(self) -> str:
        return self.sha[:7]


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)

    def out(self, *args: str) -> str:
        return self.run(*args).stdout

    def is_git(self) -> bool:
        return self.run("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"

    def toplevel(self) -> Path | None:
        top = self.run("rev-parse", "--show-toplevel").stdout.strip()
        return Path(top) if top else None

    def head(self) -> str | None:
        result = self.run("rev-parse", "--verify", "--quiet", "HEAD")
        return result.stdout.strip() or None

    def status(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for line in self.out("status", "--porcelain", "--untracked-files=all", "--", ".", *EXCLUDES).splitlines():
            if len(line) > 3:
                path = line[3:]
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                entries[path.strip('"')] = line[:2]
        return entries

    def diffs(self) -> dict[str, str]:
        args = ["diff", "--no-color", "--no-ext-diff"]
        args += ["HEAD"] if self.head() else ["--cached"]
        text = self.out(*args, "--", ".", *EXCLUDES)
        sections: dict[str, str] = {}
        current, buf = None, []
        for line in text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if current:
                    sections[current] = hashlib.sha256("".join(buf).encode()).hexdigest()
                current, buf = line.rstrip("\n").split(" b/", 1)[-1], [line]
            else:
                buf.append(line)
        if current:
            sections[current] = hashlib.sha256("".join(buf).encode()).hexdigest()
        return sections

    def remotes(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        for line in self.out("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes").splitlines():
            name, _, sha = line.partition(" ")
            refs[name] = sha
        return refs

    def snapshot(self) -> Snapshot:
        return Snapshot(head=self.head(), status=self.status(), diffs=self.diffs(), remotes=self.remotes())

    def changed_paths(self, before: Snapshot, after: Snapshot) -> list[str]:
        paths = set()
        for path in set(before.status) | set(after.status):
            if before.status.get(path) != after.status.get(path):
                paths.add(path)
        for path in set(before.diffs) | set(after.diffs):
            if before.diffs.get(path) != after.diffs.get(path):
                paths.add(path)
        if before.head != after.head and after.head:
            span = f"{before.head}..{after.head}" if before.head else after.head
            paths.update(p for p in self.out("diff", "--name-only", span).splitlines() if p)
        return sorted(paths)

    def new_commits(self, before: str | None, after: str | None) -> list[CommitInfo]:
        if not after or before == after:
            return []
        span = f"{before}..{after}" if before else after
        raw = self.out("log", "--format=%H%x1f%an%x1f%ae%x1f%s%x1f%b%x1e", span)
        commits = []
        for record in raw.split("\x1e"):
            parts = record.strip("\n").split("\x1f")
            if len(parts) == 5:
                commits.append(CommitInfo(*parts))
        return commits

    def local_identity(self) -> tuple[str | None, str | None]:
        name = self.run("config", "--local", "user.name").stdout.strip() or None
        email = self.run("config", "--local", "user.email").stdout.strip() or None
        return name, email

    def remote_url(self, name: str = "origin") -> str | None:
        return self.run("remote", "get-url", name).stdout.strip() or None

    def commits_touching(self, path: Path, *, limit: int = 1) -> list[str]:
        rel = str(Path(path).resolve().relative_to(self.root.resolve()))
        return [s for s in self.out("log", f"-n{limit}", "--format=%H", "--", rel).splitlines() if s]

    def line_commit(self, path: Path, line: int) -> str | None:
        rel = str(Path(path).resolve().relative_to(self.root.resolve()))
        out = self.out("blame", "-L", f"{line},{line}", "--porcelain", "--", rel)
        sha = out.split(" ", 1)[0] if out else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) and set(sha) != {"0"} else None


def attribution_problems(commit: CommitInfo) -> list[str]:
    return [m.group(0).strip() for m in ATTRIBUTION.finditer(f"{commit.subject}\n{commit.body}")]
