"""Terminal cell widths: wide CJK and emoji take two columns, combining marks none."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_LIST_ITEM = re.compile(r"(?:[-*+•]|\d+[.)])\s")


@lru_cache(maxsize=8192)
def char_width(ch: str) -> int:
    o = ord(ch)
    if o < 0x20 or 0x7F <= o < 0xA0 or o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F:
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(char_width(c) for c in text)


def clean(text: str) -> str:
    """Agent output can carry escape codes, tabs and carriage returns; none of them belong in a cell."""
    text = _ANSI.sub("", str(text)).replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub(" ", text)


def one_line(text: str) -> str:
    return " ".join(clean(text).split())


def _fit(text: str, cols: int) -> tuple[str, str]:
    """Split text into the longest head that fits in cols, and the rest."""
    used = 0
    for i, ch in enumerate(text):
        w = char_width(ch)
        if used + w > cols:
            return text[:i], text[i:]
        used += w
    return text, ""


def clip(text: str, cols: int, ellipsis: str = "…") -> str:
    if cols <= 0:
        return ""
    if width(text) <= cols:
        return text
    head, _ = _fit(text, max(0, cols - width(ellipsis)))
    return head + ellipsis


def pad(text: str, cols: int) -> str:
    text = clip(text, cols)
    return text + " " * (cols - width(text))


def wrap(text: str, cols: int) -> list[str]:
    """Word wrap by display width. Blank lines stay; list items get a hanging indent; long words are cut."""
    cols = max(4, cols)
    out: list[str] = []
    for para in clean(text).split("\n"):
        body = para.lstrip(" ")
        lead = para[: len(para) - len(body)]
        if not body:
            out.append("")
            continue
        if width(lead) > cols // 2:
            lead = ""
        hang = lead + ("  " if _LIST_ITEM.match(body) else "")
        line, used = lead, width(lead)
        for word in body.split(" "):
            if not word:
                continue
            w = width(word)
            gap = 1 if line.strip() else 0
            if used + gap + w <= cols:
                line += " " * gap + word
                used += gap + w
                continue
            if line.strip():
                out.append(line.rstrip())
                line, used = hang, width(hang)
            while used + w > cols:
                head, word = _fit(word, cols - used)
                if not head:
                    break
                out.append(line + head)
                line, used = hang, width(hang)
                w = width(word)
            if word:
                line += word
                used += w
        if line.strip():
            out.append(line.rstrip())
    return out


def tilde(path: object) -> str:
    """A path with the home folder written as ~, for display."""
    from pathlib import Path

    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def clock(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def tokens(n: int | None) -> str:
    if not n:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)
