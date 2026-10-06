"""Labelled messages and the three output grammars (docs/DESIGN.md section 5)."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from agent_bridge.waits import WaitSpec, is_wait_line, parse_wait_line

ORIGINS = ("owner", "planner", "supervisor", "bridge")
SENTINEL = "PROJECT COMPLETE"


@dataclass(frozen=True)
class Block:
    origin: str
    text: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown origin {self.origin!r}")


def render(blocks: list[Block]) -> str:
    """The message as delivered. It always starts with a label, so it can never begin with '-'."""
    parts = []
    for b in blocks:
        text = b.text.strip("\n")
        if not text.strip():
            continue
        head = f"[{b.origin}]" + (f" {b.note}" if b.note else "")
        if b.origin == "bridge" and not b.note:
            parts.append(f"{head} {text}")
        else:
            parts.append(f"{head}\n{text}")
    return "\n".join(parts) + "\n"


_LEAD = re.compile(r"^(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+")


def normalize(line: str) -> str:
    """A directive line without markdown decoration: headings, bullets, quotes, bold, backticks."""
    s = line.strip().replace("**", "").replace("`", "")
    s = _LEAD.sub("", s).strip()
    if len(s) >= 2 and s[0] in "*_" and s[-1] == s[0]:
        s = s[1:-1].strip()
    return s


# -- scope tokens: the vocabulary shared by SCOPE, AFFECTS and the blocked set

_ID = re.compile(r"\b([A-Z][A-Z0-9]{0,6})-(\d+[a-z]?)\b")
_PHASE = re.compile(r"\bphase\s+(\d+[a-z]?(?:\.\d+)?)", re.IGNORECASE)


def scope_tokens(text: str) -> set[str]:
    """Requirement and item ids (R-3, F-59) and numbered phases ('phase 2'), normalized."""
    tokens = {f"{m.group(1)}-{m.group(2)}" for m in _ID.finditer(text)}
    tokens |= {f"phase {m.group(1).lower()}" for m in _PHASE.finditer(text)}
    return tokens


def display_token(token: str) -> str:
    return "Phase " + token.split(" ", 1)[1] if token.startswith("phase ") else token


# -- supervisor


@dataclass
class Replan:
    phase: str | None
    problem: str


@dataclass
class SupervisorOutput:
    raw: str
    verdict: str | None = None
    reply: str | None = None
    complete: bool = False
    scope: set[str] | None = None
    scope_text: str | None = None
    scope_none: bool = False
    phase_complete: str | None = None
    wait: WaitSpec | None = None
    no_wait: bool = False
    rotate_builder: bool = False
    replan: Replan | None = None
    escalate: str | None = None
    warnings: list[str] = field(default_factory=list)
    near_misses: list[str] = field(default_factory=list)

    @property
    def has_reply(self) -> bool:
        return self.reply is not None

    def completion_text(self) -> str:
        lines = [ln for ln in self.raw.splitlines() if normalize(ln) != SENTINEL]
        return "\n".join(lines).strip()


_REPLAN = re.compile(r"^REPLAN(?:\s*\(([^)]*)\))?\s*:\s*(.*)$", re.DOTALL)
_DIRECTIVE_PREFIXES = ("VERDICT:", "SCOPE:", "PHASE COMPLETE:", "WAIT ", "NO WAIT", "ROTATE BUILDER", "REPLAN", "ESCALATE:", SENTINEL)


def _value(n: str, key: str) -> str | None:
    return n[len(key):].strip() if n.startswith(key) else None


def _near_sentinel(n: str) -> bool:
    """PROJECT COMPLETE with something attached ('PROJECT COMPLETE.', 'PROJECT COMPLETE: ...'): never taken silently."""
    return n != SENTINEL and n.upper().startswith(SENTINEL) and not n[len(SENTINEL):len(SENTINEL) + 1].isalnum()


def _sentinel_problem(n: str) -> str:
    shown = n if len(n) <= 60 else n[:57] + "..."
    return (
        f"'{shown}' was not taken as completion: if the project is complete, the first line must be exactly "
        "PROJECT COMPLETE, alone"
    )


def parse_supervisor(raw: str, *, now: datetime) -> SupervisorOutput:
    out = SupervisorOutput(raw=raw)
    lines = raw.splitlines()
    split = next((i for i, ln in enumerate(lines) if normalize(ln).startswith("REPLY:")), None)
    header = lines if split is None else lines[:split]
    if split is not None:
        first = normalize(lines[split])[len("REPLY:"):].strip()
        body = ([first] if first else []) + lines[split + 1 :]
        nonblank = [i for i, ln in enumerate(body) if ln.strip()]
        if nonblank and normalize(body[nonblank[0]]) == SENTINEL:
            out.complete = True
            body = body[: nonblank[0]] + body[nonblank[0] + 1 :]
        elif nonblank and _near_sentinel(normalize(body[nonblank[0]])):
            out.near_misses.append(_sentinel_problem(normalize(body[nonblank[0]])))
        if any(normalize(ln) == SENTINEL for ln in body):
            out.warnings.append("PROJECT COMPLETE inside the REPLY body was ignored")
        out.reply = "\n".join(body).strip()

    i = 0
    while i < len(header):
        n = normalize(header[i])
        i += 1
        if n == SENTINEL:
            out.complete = True
        elif _near_sentinel(n):
            out.near_misses.append(_sentinel_problem(n))
        elif (v := _value(n, "VERDICT:")) is not None:
            if out.verdict is None:
                out.verdict = v
        elif (v := _value(n, "SCOPE:")) is not None:
            if not v:
                # SCOPE written as a list under the label: take the items up to the next blank line or directive.
                items = []
                while i < len(header) and header[i].strip() and not normalize(header[i]).startswith(_DIRECTIVE_PREFIXES):
                    items.append(normalize(header[i]))
                    i += 1
                v = ", ".join(x for x in items if x)
            if not v:
                out.near_misses.append("SCOPE: was empty; write the requirement ids and phases on the same line, or SCOPE: none")
                continue
            out.scope_text = v
            out.scope_none = v.strip().lower().rstrip(".") in ("none", "n/a", "-", "nothing")
            out.scope = set() if out.scope_none else scope_tokens(v)
        elif (v := _value(n, "PHASE COMPLETE:")) is not None:
            out.phase_complete = v or None
        elif is_wait_line(n):
            try:
                spec = parse_wait_line(n, now=now)
            except ValueError as e:
                out.warnings.append(str(e))
                continue
            if out.wait is None:
                out.wait = spec
            else:
                out.warnings.append(f"extra WAIT ignored: {n}")
        elif n == "NO WAIT":
            out.no_wait = True
        elif n == "ROTATE BUILDER":
            out.rotate_builder = True
        elif n.startswith("REPLAN") and (m := _REPLAN.match(n)):
            problem = [m.group(2).strip()]
            while i < len(header) and header[i].strip() and not normalize(header[i]).startswith(_DIRECTIVE_PREFIXES):
                problem.append(header[i].strip())
                i += 1
            out.replan = Replan(phase=(m.group(1) or "").strip() or None, problem=" ".join(p for p in problem if p))
        elif (v := _value(n, "ESCALATE:")) is not None:
            out.escalate = v
    return out


# -- planner


@dataclass
class Question:
    number: int
    text: str
    recommended: str = ""
    why: str = ""


@dataclass
class FileBlock:
    path: str
    content: str


@dataclass
class Edit:
    path: str
    find: str
    replace: str


@dataclass
class PlannerOutput:
    raw: str
    kind: str = "unknown"  # questions | plan | change | no_change | unknown
    questions: list[Question] = field(default_factory=list)
    summary: str = ""
    files: list[FileBlock] = field(default_factory=list)
    kickoff: str = ""
    title: str = ""
    reason: str = ""
    material: bool | None = None
    affects_text: str = ""
    edits: list[Edit] = field(default_factory=list)
    ledger: str = ""
    to_supervisor: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def affects(self) -> set[str]:
        return scope_tokens(self.affects_text)


_FILE_START = re.compile(r"^===\s*FILE\s+(.+?)\s*===$")
_FILE_END = re.compile(r"^===\s*END FILE\s*===$")
_EDIT_START = re.compile(r"^===\s*EDIT\s+(.+?)\s*===$")
_EDIT_END = re.compile(r"^===\s*END EDIT\s*===$")
_FIND = re.compile(r"^---\s*FIND\s*---$")
_REPLACE = re.compile(r"^---\s*REPLACE\s*---$")
_SECTIONS = ("NO CHANGE", "CHANGE", "QUESTIONS", "PLAN", "SUMMARY", "REASON", "MATERIAL", "AFFECTS", "LEDGER", "TO SUPERVISOR", "KICKOFF")
_KINDS = {"QUESTIONS": "questions", "PLAN": "plan", "CHANGE": "change", "NO CHANGE": "no_change"}
_QNUM = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")


def _clean_path(text: str) -> str:
    return text.strip().strip("'\"`")


def parse_planner(raw: str) -> PlannerOutput:
    out = PlannerOutput(raw=raw)
    lines = raw.splitlines()
    buf: dict[str, list[str]] = defaultdict(list)
    section: str | None = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if m := _FILE_START.match(stripped):
            path, content, i = _clean_path(m.group(1)), [], i + 1
            while i < len(lines) and not _FILE_END.match(lines[i].strip()):
                content.append(lines[i])
                i += 1
            if i >= len(lines):
                out.errors.append(f"FILE {path}: missing === END FILE ===")
            out.files.append(FileBlock(path, "\n".join(content).rstrip("\n") + "\n"))
            i, section = i + 1, None
            continue
        if m := _EDIT_START.match(stripped):
            path, i = _clean_path(m.group(1)), i + 1
            find: list[str] = []
            replace: list[str] = []
            target: list[str] | None = None
            while i < len(lines) and not _EDIT_END.match(lines[i].strip()):
                s = lines[i].strip()
                if _FIND.match(s):
                    target = find
                elif _REPLACE.match(s):
                    target = replace
                elif target is not None:
                    target.append(lines[i])
                i += 1
            if i >= len(lines):
                out.errors.append(f"EDIT {path}: missing === END EDIT ===")
            if not find:
                out.errors.append(f"EDIT {path}: empty FIND")
            out.edits.append(Edit(path, "\n".join(find), "\n".join(replace)))
            i, section = i + 1, None
            continue
        n = normalize(lines[i])
        key = next((k for k in _SECTIONS if n == k or n.startswith(k + ":")), None)
        if key is not None:
            if key in _KINDS and out.kind == "unknown":
                out.kind = _KINDS[key]
            section = key
            value = n[len(key):].lstrip(":").strip()
            if value:
                buf[key].append(value)
        elif section is not None:
            buf[section].append(lines[i])
        i += 1

    def text(key: str) -> str:
        return "\n".join(buf[key]).strip()

    out.summary = text("SUMMARY")
    out.kickoff = text("KICKOFF")
    out.ledger = text("LEDGER")
    out.to_supervisor = text("TO SUPERVISOR")
    out.affects_text = text("AFFECTS")
    if out.kind == "questions":
        out.questions = _parse_questions(buf["QUESTIONS"])
        if not out.questions:
            out.errors.append("QUESTIONS: no numbered questions")
    elif out.kind == "plan":
        if not out.files:
            out.errors.append("PLAN: no === FILE blocks")
        if not out.kickoff:
            out.errors.append("PLAN: no KICKOFF")
    elif out.kind == "change":
        out.title = (buf["CHANGE"][0].strip() if buf["CHANGE"] else "")
        out.reason = text("REASON")
        material = text("MATERIAL").lower()
        if material.startswith("yes"):
            out.material = True
        elif material.startswith("no"):
            out.material = False
        else:
            out.errors.append("CHANGE: MATERIAL must be yes or no")
        if not out.edits:
            out.errors.append("CHANGE: no === EDIT blocks")
        if not out.reason:
            out.errors.append("CHANGE: no REASON")
    elif out.kind == "no_change":
        out.reason = text("NO CHANGE")
    else:
        out.errors.append("no QUESTIONS:, PLAN:, CHANGE: or NO CHANGE: marker")
    return out


def _parse_questions(lines: list[str]) -> list[Question]:
    questions: list[Question] = []
    last = "text"
    for line in lines:
        if m := _QNUM.match(line):
            questions.append(Question(int(m.group(1)), m.group(2).strip()))
            last = "text"
            continue
        if not questions:
            continue
        q = questions[-1]
        n = normalize(line)
        low = n.lower()
        if low.startswith("recommended:"):
            q.recommended, last = n.split(":", 1)[1].strip(), "recommended"
        elif low.startswith("why it matters:") or low.startswith("why:"):
            q.why, last = n.split(":", 1)[1].strip(), "why"
        elif n:
            setattr(q, last, (getattr(q, last) + " " + n).strip())
    return questions


def render_questions(questions: list[Question]) -> str:
    out = []
    for q in questions:
        out.append(f"{q.number}. {q.text}")
        if q.recommended:
            out.append(f"   Recommended: {q.recommended}")
        if q.why:
            out.append(f"   Why it matters: {q.why}")
    return "\n".join(out)


# -- builder report


@dataclass
class BuilderSignals:
    wait: WaitSpec | None = None
    wait_error: str | None = None
    decisions_needed: list[str] = field(default_factory=list)
    phase_claims: list[str] = field(default_factory=list)


_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


def parse_builder(text: str, *, now: datetime, phase_pattern: re.Pattern[str]) -> BuilderSignals:
    sig = BuilderSignals()
    lines = text.splitlines()
    for line in lines:
        n = normalize(line)
        if is_wait_line(n):
            try:
                sig.wait = parse_wait_line(n, now=now)
                sig.wait_error = None
            except ValueError as e:
                sig.wait_error = str(e)
    start = next((i for i, ln in enumerate(lines) if normalize(ln).upper().startswith("DECISIONS NEEDED")), None)
    if start is not None:
        inline = normalize(lines[start]).split(":", 1)[1].strip() if ":" in lines[start] else ""
        none = inline.lower().rstrip(".") in ("none", "n/a", "no", "nothing")
        if inline and not none:
            sig.decisions_needed.append(inline)
        if not none:
            # The list right under the label only: it ends at the first blank line after an item, a heading,
            # a non-item line, or PROCEEDING / WAIT.
            for line in lines[start + 1 :]:
                n = normalize(line)
                if n.upper().startswith(("PROCEEDING", "WAIT ")) or line.lstrip().startswith("#"):
                    break
                if not line.strip():
                    if sig.decisions_needed:
                        break
                    continue
                if m := _ITEM.match(line):
                    sig.decisions_needed.append(m.group(1).strip())
                elif not line.startswith((" ", "\t")):
                    break
    sig.phase_claims = [m.group(0) for m in phase_pattern.finditer(text)]
    return sig
