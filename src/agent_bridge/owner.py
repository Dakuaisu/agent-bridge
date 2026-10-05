"""The owner to-do (`review`) and the OWNER_REVIEW.md template (docs/DESIGN.md 6.10, section 11).

Reads every ledger and open-items format the three migrated projects use, plus agent-bridge's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_bridge.contract import headings
from agent_bridge.protocol import display_token

_ID = re.compile(r"\b([A-Z][A-Z0-9]{0,6}-\d+[a-z]?)\b")
_STATUS = re.compile(r"^\W*Status:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_REVIEWED = re.compile(r"APPROVED|REJECTED|OWNER DECISION|SUPERSEDED|RESOLVED", re.IGNORECASE)


@dataclass
class TodoItem:
    kind: str  # decision | blocked | change
    ident: str
    title: str
    path: Path | None = None
    line: int | None = None
    detail: str = ""

    def where(self, repo: Path) -> str:
        if self.path is None:
            return ""
        try:
            rel = self.path.relative_to(repo)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}" if self.line else str(rel)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _sections(text: str) -> list[tuple[int, str, str]]:
    """(line number, heading title, the heading's own text up to the next heading of any level)."""
    lines = text.splitlines()
    heads = headings(text)
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].line if i + 1 < len(heads) else len(lines)
        out.append((h.line + 1, h.title, "\n".join(lines[h.line + 1 : end])))
    return out


def _ident(title: str, line: int) -> str:
    m = _ID.search(title)
    return m.group(1) if m else f"line {line}"


def _clean_title(title: str) -> str:
    title = re.sub(r"\bAUTONOMOUS DECISION\s*-\s*owner to review:?\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\s*(?:DEC-\d+|[A-Z][A-Z0-9]{0,6}-\d+[a-z]?)\s*", "", title)
    return title.strip(" -—:") or title


def ledger_items(path: Path) -> list[TodoItem]:
    items = []
    for line, title, body in _sections(_read(path)):
        status_m = _STATUS.search(body)
        status = status_m.group(1).strip() if status_m else ""
        autonomous = "AUTONOMOUS DECISION" in title.upper() or "AUTONOMOUS DECISION" in status.upper()
        awaiting = "AWAITING OWNER" in status.upper()
        if not (autonomous or awaiting):
            continue
        reviewed = bool(re.search(r"(?im)^\W*owner review\b", body)) or (bool(status) and bool(_REVIEWED.search(status)) and "AUTONOMOUS" not in status.upper())
        if reviewed:
            continue
        decided = re.search(r"(?im)^\W*Decided by:\s*(.+)$", body)
        items.append(
            TodoItem(
                kind="change" if awaiting else "decision",
                ident=_ident(title, line),
                title=_clean_title(title),
                path=path,
                line=line,
                detail=decided.group(1).strip() if decided else "",
            )
        )
    return items


def open_items(path: Path) -> list[TodoItem]:
    text = _read(path)
    items = []
    for line, title, body in _sections(text):
        status_m = _STATUS.search(body)
        status = status_m.group(1) if status_m else ""
        blocked = "OWNER-BLOCKED" in title.upper() or "OWNER-BLOCKED" in status.upper()
        resolved = "RESOLVED" in title.upper() or "RESOLVED" in status.upper()
        if blocked and not resolved:
            items.append(TodoItem("blocked", _ident(title, line), re.sub(r"[:(]?\s*OWNER-BLOCKED.*$", "", title).strip(" :-—") or title, path, line))
    for i, row in enumerate(text.splitlines(), 1):
        if row.lstrip().startswith("|") and "OWNER-BLOCKED" in row.upper() and "RESOLVED" not in row.upper():
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) >= 2:
                summary = re.sub(r"^OWNER-BLOCKED:?\s*", "", cells[1])
                items.append(TodoItem("blocked", cells[0] or f"line {i}", summary[:160] + ("..." if len(summary) > 160 else ""), path, i))
    return items


def change_items(changes: list[dict[str, Any]]) -> list[TodoItem]:
    return [
        TodoItem(
            "change",
            c["id"],
            c.get("title", ""),
            detail=f"affects {', '.join(display_token(t) for t in c.get('affects', [])) or '?'}; diff {c.get('diff')}",
        )
        for c in changes
        if c.get("status") == "awaiting"
    ]


def collect(decisions: Path, open_docs: list[Path], changes: list[dict[str, Any]]) -> dict[str, list[TodoItem]]:
    ledger = ledger_items(decisions)
    return {
        "decisions": [i for i in ledger if i.kind == "decision"],
        "blocked": [item for doc in open_docs for item in open_items(doc)],
        "changes": change_items(changes),
    }


def render_todo(project: str, repo: Path, found: dict[str, list[TodoItem]], now: datetime) -> str:
    out = [f"# Owner to-do: {project} ({now:%Y-%m-%d %H:%M})", ""]
    out += [f"## Decisions to review ({len(found['decisions'])})", ""]
    for i in found["decisions"]:
        by = f"; decided by {i.detail}" if i.detail else ""
        out.append(f"- [ ] {i.ident} {i.title} ({i.where(repo)}{by})")
    out += ["", f"## OWNER-BLOCKED items ({len(found['blocked'])})", ""]
    out += [f"- [ ] {i.ident} {i.title} ({i.where(repo)})" for i in found["blocked"]]
    out += ["", f"## Plan changes waiting for you ({len(found['changes'])})", ""]
    out += [
        f"- [ ] {i.ident} {i.title} ({i.detail}): `agent-bridge approve {i.ident}`, or `agent-bridge approve --reject {i.ident} --reason \"...\"`"
        for i in found["changes"]
    ]
    out += ["", "Write your decisions in an OWNER_REVIEW.md (`agent-bridge review --template docs/OWNER_REVIEW.md`), then `agent-bridge decide docs/OWNER_REVIEW.md`.", ""]
    return "\n".join(out)


def review_template(repo: Path, found: dict[str, list[TodoItem]], now: datetime) -> str:
    out = [
        f"# Owner review — {now:%Y-%m-%d}",
        "",
        "The decisions below are OWNER DECISIONS. Where one conflicts with the PRD, the decision wins; the planner",
        "records each one in the ledger as an OWNER DECISION. The project's rules still apply.",
        "",
    ]
    for n, i in enumerate(found["decisions"], 1):
        out += [f"## D{n} — {i.title} ({i.ident}, {i.where(repo)})", "", "Decision: accept as is | change: ...", ""]
    if not found["decisions"]:
        out += ["## D1 — <your decision>", "", "<what to do>", ""]
    if found["blocked"]:
        out += ["## OWNER-BLOCKED items", ""]
        out += [f"- {i.ident} {i.title}: <done by the owner / still blocked / decision>" for i in found["blocked"]]
        out.append("")
    out += ["## Done when", ""]
    count = max(1, len(found["decisions"]))
    out += [f"- [ ] D{n} applied, committed, and recorded in the ledger" for n in range(1, count + 1)]
    out += ["- [ ] Tests pass; the open items and the ledger agree", ""]
    return "\n".join(out)
