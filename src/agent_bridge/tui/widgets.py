"""Keys, text inputs and lists, independent of curses drawing so they can be tested directly."""

from __future__ import annotations

import curses

from agent_bridge.tui.text import char_width, width

_SPECIAL = {
    curses.KEY_UP: "up",
    curses.KEY_DOWN: "down",
    curses.KEY_LEFT: "left",
    curses.KEY_RIGHT: "right",
    curses.KEY_HOME: "home",
    curses.KEY_END: "end",
    curses.KEY_PPAGE: "pgup",
    curses.KEY_NPAGE: "pgdn",
    curses.KEY_BACKSPACE: "backspace",
    curses.KEY_DC: "delete",
    curses.KEY_ENTER: "enter",
    curses.KEY_BTAB: "shift-tab",
    curses.KEY_RESIZE: "resize",
    curses.KEY_F1: "f1",
}


def keyname(k: str | int) -> str:
    if isinstance(k, int):
        return _SPECIAL.get(k, f"key-{k}")
    if k in ("\n", "\r"):
        return "enter"
    if k == "\t":
        return "tab"
    if k == "\x1b":
        return "esc"
    if k in ("\x7f", "\x08"):
        return "backspace"
    if len(k) == 1 and ord(k) < 32:
        return "ctrl-" + chr(ord(k) + 96)
    return k


def printable(name: str) -> bool:
    return len(name) == 1 and name.isprintable()


class LineEdit:
    def __init__(self, text: str = "", placeholder: str = "") -> None:
        self.text = text
        self.pos = len(text)
        self.placeholder = placeholder

    def set(self, text: str) -> None:
        self.text, self.pos = text, len(text)

    def insert(self, text: str) -> None:
        self.text = self.text[: self.pos] + text + self.text[self.pos :]
        self.pos += len(text)

    def key(self, name: str) -> bool:
        t, p = self.text, self.pos
        if printable(name):
            self.text, self.pos = t[:p] + name + t[p:], p + 1
        elif name == "left":
            self.pos = max(0, p - 1)
        elif name == "right":
            self.pos = min(len(t), p + 1)
        elif name in ("home", "ctrl-a"):
            self.pos = 0
        elif name in ("end", "ctrl-e"):
            self.pos = len(t)
        elif name == "backspace":
            if p:
                self.text, self.pos = t[: p - 1] + t[p:], p - 1
        elif name in ("delete", "ctrl-d"):
            self.text = t[:p] + t[p + 1 :]
        elif name == "ctrl-u":
            self.text, self.pos = t[p:], 0
        elif name == "ctrl-k":
            self.text = t[:p]
        elif name == "ctrl-w":
            head = t[:p].rstrip()
            cut = head.rfind(" ") + 1
            self.text, self.pos = t[:cut] + t[p:], cut
        else:
            return False
        return True

    def view(self, cols: int) -> tuple[str, int]:
        """The visible slice and the cursor column inside it, scrolling so the cursor stays in view."""
        cols = max(1, cols)
        start = 0
        while width(self.text[start : self.pos]) > cols - 1:
            start += 1
        out, used = [], 0
        for ch in self.text[start:]:
            w = char_width(ch)
            if used + w > cols:
                break
            out.append(ch)
            used += w
        return "".join(out), width(self.text[start : self.pos])


class TextArea:
    """Multi-line input with soft wrapping by display width."""

    def __init__(self, text: str = "", placeholder: str = "") -> None:
        self.lines = text.split("\n") if text else [""]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[-1])
        self.top = 0
        self.cols = 60
        self.placeholder = placeholder

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def set(self, text: str) -> None:
        self.__init__(text, self.placeholder)  # type: ignore[misc]

    def insert(self, text: str) -> None:
        """Insert pasted text at the cursor in one step (a long paste key by key is slow)."""
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        line = self.lines[self.row]
        head, tail = line[: self.col], line[self.col :]
        if len(parts) == 1:
            self.lines[self.row] = head + parts[0] + tail
            self.col += len(parts[0])
            return
        new = [head + parts[0], *parts[1:-1], parts[-1] + tail]
        self.lines[self.row : self.row + 1] = new
        self.row += len(parts) - 1
        self.col = len(parts[-1])

    def visual(self, cols: int | None = None) -> list[tuple[int, int, int]]:
        """(line index, start, end) per screen row."""
        cols = max(2, cols or self.cols)
        rows = []
        for i, line in enumerate(self.lines):
            start, used = 0, 0
            for j, ch in enumerate(line):
                w = char_width(ch)
                if used + w > cols:
                    rows.append((i, start, j))
                    start, used = j, 0
                used += w
            rows.append((i, start, len(line)))
            if line and used == cols:
                rows.append((i, len(line), len(line)))
        return rows

    def cursor(self, cols: int | None = None) -> tuple[int, int]:
        rows = self.visual(cols)
        for v, (i, s, e) in enumerate(rows):
            if i != self.row:
                continue
            last = v + 1 == len(rows) or rows[v + 1][0] != i
            if s <= self.col < e or (self.col == e and last):
                return v, width(self.lines[i][s : self.col])
        return len(rows) - 1, 0

    def _to_visual(self, v: int, col: int) -> None:
        rows = self.visual()
        v = max(0, min(len(rows) - 1, v))
        i, s, e = rows[v]
        used, pos = 0, s
        while pos < e and used + char_width(self.lines[i][pos]) <= col:
            used += char_width(self.lines[i][pos])
            pos += 1
        self.row, self.col = i, pos

    def key(self, name: str) -> bool:
        line = self.lines[self.row]
        c = self.col
        if printable(name):
            self.lines[self.row] = line[:c] + name + line[c:]
            self.col += 1
        elif name == "enter":
            self.lines[self.row : self.row + 1] = [line[:c], line[c:]]
            self.row, self.col = self.row + 1, 0
        elif name == "backspace":
            if c:
                self.lines[self.row] = line[: c - 1] + line[c:]
                self.col -= 1
            elif self.row:
                prev = self.lines[self.row - 1]
                self.lines[self.row - 1 : self.row + 1] = [prev + line]
                self.row, self.col = self.row - 1, len(prev)
        elif name == "delete":
            if c < len(line):
                self.lines[self.row] = line[:c] + line[c + 1 :]
            elif self.row + 1 < len(self.lines):
                self.lines[self.row : self.row + 2] = [line + self.lines[self.row + 1]]
        elif name == "left":
            if c:
                self.col -= 1
            elif self.row:
                self.row -= 1
                self.col = len(self.lines[self.row])
        elif name == "right":
            if c < len(line):
                self.col += 1
            elif self.row + 1 < len(self.lines):
                self.row, self.col = self.row + 1, 0
        elif name in ("home", "ctrl-a"):
            self.col = 0
        elif name == "end":
            self.col = len(line)
        elif name == "ctrl-k":
            self.lines[self.row] = line[:c]
        elif name == "ctrl-u":
            self.lines[self.row] = line[c:]
            self.col = 0
        elif name in ("up", "down", "pgup", "pgdn"):
            v, col = self.cursor()
            step = {"up": -1, "down": 1, "pgup": -8, "pgdn": 8}[name]
            last = len(self.visual()) - 1
            if (step < 0 and v == 0) or (step > 0 and v == last):
                return False
            self._to_visual(v + step, col)
        else:
            return False
        return True

    def scroll_to_cursor(self, rows: int) -> None:
        v, _ = self.cursor()
        if v < self.top:
            self.top = v
        elif v >= self.top + rows:
            self.top = v - rows + 1


class ListState:
    def __init__(self) -> None:
        self.index = 0
        self.top = 0

    def key(self, name: str, n: int, page: int = 10) -> bool:
        if n <= 0:
            return False
        step = {"up": -1, "down": 1, "k": -1, "j": 1, "pgup": -page, "pgdn": page}.get(name)
        if step is not None:
            self.index = max(0, min(n - 1, self.index + step))
        elif name in ("home", "g"):
            self.index = 0
        elif name in ("end", "G"):
            self.index = n - 1
        else:
            return False
        return True

    def clamp(self, n: int, rows: int) -> None:
        self.index = max(0, min(self.index, n - 1)) if n else 0
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + rows:
            self.top = self.index - rows + 1
        self.top = max(0, min(self.top, max(0, n - rows)))
