"""The owner-review report (docs/DESIGN.md 6.10). Every fact comes from the repo, the ledger, the
state and the logs; nothing is taken from an agent's prose."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_bridge import owner
from agent_bridge.config import Config, format_duration
from agent_bridge.journal import read_events
from agent_bridge.protocol import display_token
from agent_bridge.repo import Repo
from agent_bridge.statedir import StateDir, atomic_write_text, read_json

WARNING_GROUPS = [
    ("Tripwire: a read-only role's turn changed the repo or used write tools", re.compile(r"TURN CHANGED THE REPO|USED WRITE TOOLS")),
    ("Served-model mismatches", re.compile(r"MODEL MISMATCH")),
    ("Contract drift", re.compile(r"CONTRACT CHANGED")),
    ("Danger commands", re.compile(r"DANGER COMMAND")),
    ("Commit attribution and authors", re.compile(r"COMMIT (?:ATTRIBUTION|AUTHOR)|REMOTE REFS MOVED")),
    ("Question tool aborts", re.compile(r"QUESTION TOOL")),
    ("Usage limits", re.compile(r"USAGE LIMIT")),
    ("Pauses", re.compile(r"PAUSED")),
]
_REVIEW_ENTRY = re.compile(r"^=== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*?) ===$", re.MULTILINE)


def github_base(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+)/(.+?)(?:\.git)?$", url.strip())
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


class Linker:
    def __init__(self, repo: Repo, report_dir: Path) -> None:
        self.repo = repo
        self.report_dir = report_dir
        self.github = github_base(repo.remote_url())

    def file(self, path: Path | None, line: int | None) -> str:
        if path is None:
            return ""
        try:
            rel = path.resolve().relative_to(self.repo.root.resolve())
        except ValueError:
            rel = path
        target = os.path.relpath(path.resolve(), self.report_dir.resolve())
        anchor = f"#L{line}" if line else ""
        label = f"{rel}:{line}" if line else str(rel)
        return f"[{label}]({target}{anchor})"

    def commit(self, sha: str | None) -> str:
        if not sha:
            return "not committed yet"
        short = sha[:7]
        return f"[{short}]({self.github}/commit/{sha})" if self.github else short

    def line_commit(self, path: Path | None, line: int | None) -> str:
        if path is None or not line or not path.exists():
            return self.commit(None)
        return self.commit(self.repo.line_commit(path, line))

    def text_commit(self, path: Path, text: str) -> str:
        needle = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 8), "")
        if not needle:
            return self.commit(None)
        rel = str(path.resolve().relative_to(self.repo.root.resolve()))
        sha = self.repo.out("log", "-n1", "--format=%H", "-S", needle, "--", rel).strip()
        return self.commit(sha or None)


def parse_results(path: Path) -> tuple[list[dict[str, str]], str | None]:
    """Rows of the results register; the second value explains a missing or unreadable register."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], f"there is no results register ({path.name} does not exist)"
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        rows.append({header[i] if i < len(header) else f"col{i}": c for i, c in enumerate(cells)})
    if not rows:
        return [], f"the results register ({path.name}) has no rows"
    return rows, None


def _col(row: dict[str, str], *names: str) -> str:
    for key, value in row.items():
        if any(n in key for n in names):
            return value
    return ""


def review_entries(path: Path, since: str | None) -> list[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    cutoff = since[:19].replace("T", " ") if since else ""
    return [(m.group(1), m.group(2)) for m in _REVIEW_ENTRY.finditer(text) if m.group(1) >= cutoff]


def write_report(cfg: Config, sd: StateDir, state: Any, *, now: datetime, out: Path | None = None) -> Path:
    sd.reports.mkdir(parents=True, exist_ok=True)
    path = out or sd.reports / f"{now:%Y%m%d-%H%M%S}.md"
    atomic_write_text(path, render_report(cfg, sd, state, now=now, report_dir=path.parent))
    if out is None:
        atomic_write_text(sd.report, render_report(cfg, sd, state, now=now, report_dir=sd.report.parent))
    return path


def render_report(cfg: Config, sd: StateDir, state: Any, *, now: datetime, report_dir: Path) -> str:
    repo = Repo(cfg.project.repo)
    link = Linker(repo, report_dir)
    contract_info = state.contract or {}
    changes = [read_json(p) for p in sorted((sd.plan / "changes").glob("PC-*.json"))]
    found = owner.collect(cfg.project.decisions, [cfg.project.open_items], [c for c in changes if isinstance(c, dict)])
    events = read_events(sd.events)
    first = next((e["ts"] for e in events), None)
    lines = [f"# Owner-review report: {cfg.project.name}", "", f"Generated {now:%Y-%m-%d %H:%M} by agent-bridge from the repo, the ledger and `.bridge/`.", ""]

    lines += ["## 1. Summary", ""]
    complete = state.complete or {}
    lines.append(f"- State: {state.phase}" + (f" (PROJECT COMPLETE at exchange {complete.get('exchange')}, {complete.get('at')})" if complete else ""))
    if contract_info:
        lines.append(f"- Contract approved {contract_info.get('approved_at')} by {contract_info.get('by')}" + (" under --auto-approve" if contract_info.get("auto_approve") else ""))
    else:
        lines.append("- Contract: not approved")
    for done in state.phases_done:
        dec = f", {done['dec']}" if done.get("dec") else ""
        lines.append(f"- {done['phase']}: verified complete by the supervisor at exchange {done['exchange']} ({done['at']}{dec})")
    lines.append(f"- Exchanges: {state.exchange}")
    if first:
        started = datetime.fromisoformat(first)
        lines.append(f"- Wall time since the first launch: {format_duration(int((now - started).total_seconds()))} (from {first})")
    for role in ("planner", "supervisor", "builder"):
        r = cfg.role(role)
        enforced = "n/a (writes)" if role == "builder" else ("enforced: --tools Read,Grep,Glob" if r.engine == "claude-code" else "read-only by instruction only")
        lines.append(f"- {role}: {r.engine} {r.engine_model()}; read-only: {enforced}")
    warnings = review_entries(sd.review_log, contract_info.get("approved_at"))
    lines += [f"- Warnings in review.log since approval: {len(warnings)}", ""]

    lines += [f"## 2. Decisions to review ({len(found['decisions'])})", ""]
    for i in found["decisions"]:
        by = f" Decided by: {i.detail}." if i.detail else ""
        lines.append(f"- [ ] **{i.ident}** {i.title}.{by} {link.file(i.path, i.line)}, commit {link.line_commit(i.path, i.line)}")
    if not found["decisions"]:
        lines.append("None.")
    lines.append("")

    lines += [f"## 3. OWNER-BLOCKED items ({len(found['blocked'])})", ""]
    for i in found["blocked"]:
        lines.append(f"- [ ] **{i.ident}** {i.title}. {link.file(i.path, i.line)}, commit {link.line_commit(i.path, i.line)}")
    if not found["blocked"]:
        lines.append("None.")
    lines.append("")

    lines += [f"## 4. Plan changes ({len(changes)})", ""]
    for c in changes:
        if not isinstance(c, dict):
            continue
        affects = ", ".join(display_token(t) for t in c.get("affects", [])) or "?"
        ledger = link.file(cfg.project.decisions, c.get("dec_line"))
        if c.get("status") in ("applied", "autonomous", "owner", "approved") and c.get("edits"):
            edit = c["edits"][0]
            target = cfg.project.repo / edit["path"]
            carried = link.text_commit(target, "\n".join(ln for ln in edit["replace"].splitlines() if ln not in edit["find"].splitlines()) or edit["replace"])
        else:
            carried = "not applied"
        diff_path = cfg.project.repo / c.get("diff", "")
        lines.append(
            f"- **{c['id']}** {c.get('title')}: {c.get('status')}; affects {affects}; {c.get('dec')} {ledger}; "
            f"diff {link.file(diff_path, None)}; commit {carried}"
        )
    if not changes:
        lines.append("None.")
    lines.append("")

    rows, missing = parse_results(cfg.project.results)
    lines += ["## 5. Results: development vs real", ""]
    if missing:
        lines += [f"No results to report: {missing}.", ""]
    else:
        groups: dict[str, list[dict[str, str]]] = {"real": [], "development": [], "unlabelled": []}
        for row in rows:
            kind = _col(row, "kind").lower()
            key = "real" if kind.startswith("real") else "development" if kind.startswith("dev") else "unlabelled"
            groups[key].append(row)
        for key, title in (("real", "Real results"), ("development", "Development results (never presented as real)"), ("unlabelled", "Rows with no valid kind (fix the register)")):
            lines += [f"### {title} ({len(groups[key])})", ""]
            for row in groups[key]:
                lines.append(
                    f"- {_col(row, 'id') or '?'}: {_col(row, 'what', 'measure', 'metric') or '?'} = {_col(row, 'value') or '?'}; "
                    f"run {_col(row, 'run', 'artifact') or '?'}; commit {_col(row, 'commit') or '?'}"
                )
            if not groups[key]:
                lines.append("None.")
            lines.append("")

    lines += ["## 6. Warnings since the contract was approved", ""]
    grouped: dict[str, list[tuple[str, str]]] = {}
    for ts, title in warnings:
        name = next((n for n, rx in WARNING_GROUPS if rx.search(title)), "Other")
        grouped.setdefault(name, []).append((ts, title))
    for name, entries in grouped.items():
        lines += [f"### {name} ({len(entries)})", ""] + [f"- {ts} {title}" for ts, title in entries[-20:]] + [""]
    if not grouped:
        lines += ["None.", ""]
    lines.append(f"Full detail: {link.file(sd.review_log, None)} and {link.file(sd.loop_log, None)}.")
    return "\n".join(lines) + "\n"
