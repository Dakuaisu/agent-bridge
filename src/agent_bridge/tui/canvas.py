"""An off-screen cell buffer. Screens draw here; the app copies changed rows to curses."""

from __future__ import annotations

import html
from dataclasses import replace
from functools import lru_cache

from agent_bridge.tui.text import char_width, clip, width
from agent_bridge.tui.theme import DEFAULT, INHERIT, Style, xterm_rgb

PLAIN = Style()
ROUND = "╭╮╰╯─│"
SQUARE = "┌┐└┘─│"
HEAVY = "┏┓┗┛━┃"


@lru_cache(maxsize=4096)
def _merge(style: Style, under_bg: int) -> Style:
    return replace(style, bg=under_bg) if style.bg == INHERIT else style


class Canvas:
    def __init__(self, h: int, w: int, bg: int = DEFAULT) -> None:
        self.h, self.w = max(0, h), max(0, w)
        base = Style(DEFAULT, bg)
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.styles = [[base] * self.w for _ in range(self.h)]
        self.cursor: tuple[int, int] | None = None

    # ------------------------------------------------------------- primitives

    def _unwide(self, y: int, x: int) -> None:
        """Before overwriting a cell, break any wide character it belongs to."""
        row = self.chars[y]
        if row[x] == "" and x > 0:
            row[x - 1] = " "
        elif x + 1 < self.w and row[x + 1] == "" and char_width(row[x] or " ") == 2:
            row[x + 1] = " "

    def put(self, y: int, x: int, text: str, style: Style = PLAIN, max_x: int | None = None) -> int:
        """Write text at (y, x), clipped at max_x (exclusive) or the edge; returns the next column."""
        end = self.w if max_x is None else min(max_x, self.w)
        if not 0 <= y < self.h:
            return x + width(text)
        row, styles = self.chars[y], self.styles[y]
        for ch in text:
            cw = char_width(ch)
            if cw == 0:
                continue
            if x + cw > end:
                break
            if x >= 0:
                self._unwide(y, x)
                row[x] = ch
                styles[x] = _merge(style, styles[x].bg)
                if cw == 2:
                    self._unwide(y, x + 1)
                    row[x + 1] = ""
                    styles[x + 1] = _merge(style, styles[x + 1].bg)
            x += cw
        return x

    def fill(self, y: int, x: int, h: int, w: int, style: Style, ch: str = " ") -> None:
        for yy in range(max(0, y), min(self.h, y + h)):
            row, styles = self.chars[yy], self.styles[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                self._unwide(yy, xx)
                row[xx] = ch
                styles[xx] = _merge(style, styles[xx].bg)

    def tint(self, y: int, x: int, h: int, w: int, bg: int, fg: int | None = None) -> None:
        """Repaint the background (and optionally the foreground) of a region, keeping its text: shadows and selections."""
        for yy in range(max(0, y), min(self.h, y + h)):
            styles = self.styles[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                s = styles[xx]
                styles[xx] = replace(s, bg=bg, fg=s.fg if fg is None else fg)

    def hline(self, y: int, x: int, w: int, style: Style, ch: str = "─") -> None:
        self.put(y, x, ch * max(0, w), style)

    def vline(self, y: int, x: int, h: int, style: Style, ch: str = "│") -> None:
        for yy in range(y, y + h):
            self.put(yy, x, ch, style)

    def box(
        self,
        y: int,
        x: int,
        h: int,
        w: int,
        style: Style,
        *,
        title: str | None = None,
        title_style: Style | None = None,
        right: str | None = None,
        right_style: Style | None = None,
        chars: str = ROUND,
    ) -> None:
        if h < 2 or w < 2:
            return
        tl, tr, bl, br, hz, vt = chars
        self.put(y, x, tl + hz * (w - 2) + tr, style)
        self.put(y + h - 1, x, bl + hz * (w - 2) + br, style)
        for yy in range(y + 1, y + h - 1):
            self.put(yy, x, vt, style)
            self.put(yy, x + w - 1, vt, style)
        if title and w > 6:
            self.put(y, x + 2, clip(title, w - 4), title_style or style)
        if right and w > 10:
            text = clip(right, max(0, w - 6 - (width(title) if title else 0)))
            self.put(y, x + w - 2 - width(text), text, right_style or style)

    def panel(self, y: int, x: int, h: int, w: int, border: Style, bg: int, **kw: object) -> None:
        self.fill(y, x, h, w, Style(DEFAULT, bg))
        self.box(y, x, h, w, border.on(bg) if border.bg == INHERIT else border, **kw)  # type: ignore[arg-type]

    def shadow(self, y: int, x: int, h: int, w: int) -> None:
        self.tint(y + h, x + 2, 1, w, 232, 238)
        self.tint(y + 1, x + w, h, 2, 232, 238)

    # ------------------------------------------------------------- output

    def runs(self, y: int) -> list[tuple[int, str, Style]]:
        """Consecutive cells with one style, as (x, text, style); wide characters fill two cells."""
        out: list[tuple[int, str, Style]] = []
        row, styles = self.chars[y], self.styles[y]
        start, buf, cur = 0, [], None
        for x in range(self.w):
            ch = row[x]
            if ch == "":
                continue
            s = styles[x]
            if s != cur:
                if buf:
                    out.append((start, "".join(buf), cur))  # type: ignore[arg-type]
                start, buf, cur = x, [], s
            buf.append(ch)
        if buf:
            out.append((start, "".join(buf), cur))  # type: ignore[arg-type]
        return out

    def text(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.chars]

    def find(self, needle: str) -> tuple[int, int] | None:
        for y, line in enumerate(self.text()):
            x = line.find(needle)
            if x >= 0:
                return y, x
        return None

    def to_html(self, title: str = "agent-bridge") -> str:
        """A static picture of the screen, for previews and docs."""

        def css(s: Style) -> str:
            fg = s.fg if s.fg >= 0 else 252
            bg = s.bg if s.bg >= 0 else 233
            if s.reverse:
                fg, bg = bg, fg
            r, g, b = xterm_rgb(fg)
            parts = [f"color:rgb({r},{g},{b})"]
            r, g, b = xterm_rgb(bg)
            parts.append(f"background:rgb({r},{g},{b})")
            if s.bold:
                parts.append("font-weight:700")
            if s.dim:
                parts.append("opacity:.7")
            if s.underline:
                parts.append("text-decoration:underline")
            return ";".join(parts)

        lines = []
        for y in range(self.h):
            spans = []
            col = 0
            for x, text, s in self.runs(y):
                spans.append(f'<span style="{css(s)}">{html.escape(text)}</span>')
                col = x + width(text)
            lines.append("".join(spans) + " " * max(0, self.w - col))
        body = "\n".join(lines)
        r, g, b = xterm_rgb(233)
        return (
            f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<body style='margin:0;background:rgb({r},{g},{b})'>"
            "<pre style=\"margin:0;padding:14px;font:15px/1.13 'SF Mono',Menlo,Monaco,monospace;"
            f"letter-spacing:0;white-space:pre\">{body}</pre></body>"
        )
