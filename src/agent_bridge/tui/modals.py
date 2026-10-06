"""Overlays: forms, confirmations, the document viewer, the command palette and pick lists."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agent_bridge.tui import theme as T
from agent_bridge.tui.canvas import Canvas
from agent_bridge.tui.draw import parts
from agent_bridge.tui.text import clip, one_line, pad, width, wrap
from agent_bridge.tui.theme import Style
from agent_bridge.tui.widgets import LineEdit, ListState, TextArea

if TYPE_CHECKING:
    from agent_bridge.tui.app import App


class Modal:
    full = False

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        raise NotImplementedError

    def key(self, name: str, app: App) -> None:
        raise NotImplementedError


def scrim(cv: Canvas) -> None:
    cv.tint(0, 0, cv.h, cv.w, 232, 238)


def frame(cv: Canvas, title: str, w: int, h: int, accent: int = 51) -> tuple[int, int, int, int]:
    w = max(24, min(w, cv.w - 4))
    h = max(5, min(h, cv.h - 2))
    y = max(1, (cv.h - h) // 2)
    x = max(0, (cv.w - w) // 2)
    cv.shadow(y, x, h, w)
    cv.panel(y, x, h, w, Style(accent), T.MODAL_BG, title=f" {title} ", title_style=Style(accent, bold=True))
    return y, x, h, w


def button_part(label: str, focused: bool, tone: str, accent: int) -> tuple[str, Style]:
    if focused:
        colour = 203 if tone == "danger" else accent
        return f"  {label}  ", Style(16, colour, bold=True)
    if tone == "primary":
        return f"  {label}  ", Style(accent, 239, bold=True)
    if tone == "danger":
        return f"  {label}  ", Style(203, 239, bold=True)
    return f"  {label}  ", Style(250, 239)


def hints(cv: Canvas, y: int, x: int, items: list[tuple[str, str]], max_x: int) -> None:
    out: list[tuple[str, Style]] = []
    for k, label in items:
        out += [(k, Style(250, bold=True)), (f" {label}   ", T.FAINT)]
    parts(cv, y, x, out, max_x)


# ----------------------------------------------------------------- form fields


class Field:
    focusable = True
    wide = False

    def __init__(self, label: str = "", when: Callable[[], bool] | None = None) -> None:
        self.label = label
        self.when = when

    def visible(self) -> bool:
        return self.when() if self.when else True

    def rows(self, w: int) -> int:
        return 1

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        return None

    def key(self, name: str) -> bool:
        return False


class LineField(Field):
    def __init__(self, label: str, value: str = "", placeholder: str = "", when: Callable[[], bool] | None = None) -> None:
        super().__init__(label, when)
        self.edit = LineEdit(value, placeholder)

    @property
    def value(self) -> str:
        return self.edit.text

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        bg = 239 if focused else T.FIELD_BG
        cv.fill(y, x, 1, w, Style(T.DEFAULT, bg))
        text, cur = self.edit.view(w - 2)
        if not self.edit.text and self.edit.placeholder:
            cv.put(y, x + 1, clip(self.edit.placeholder, w - 2), Style(243, bg))
        else:
            cv.put(y, x + 1, text, Style(255, bg))
        return (y, x + 1 + cur) if focused else None

    def key(self, name: str) -> bool:
        return self.edit.key(name)


class TextField(Field):
    def __init__(self, label: str, value: str = "", rows: int = 5, placeholder: str = "", when: Callable[[], bool] | None = None) -> None:
        super().__init__(label, when)
        self.area = TextArea(value, placeholder)
        self.n = rows

    @property
    def value(self) -> str:
        return self.area.text

    def rows(self, w: int) -> int:
        return self.n

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        bg = 239 if focused else T.FIELD_BG
        cv.fill(y, x, self.n, w, Style(T.DEFAULT, bg))
        a = self.area
        a.cols = max(4, w - 3)
        if not a.text and a.placeholder:
            for i, line in enumerate(wrap(a.placeholder, a.cols)[: self.n]):
                cv.put(y + i, x + 1, line, Style(243, bg))
        a.scroll_to_cursor(self.n)
        rows = a.visual()
        for i, (li, s, e) in enumerate(rows[a.top : a.top + self.n]):
            cv.put(y + i, x + 1, a.lines[li][s:e], Style(255, bg))
        if len(rows) > self.n:
            frac = a.top / max(1, len(rows) - self.n)
            cv.put(y + int(frac * (self.n - 1)), x + w - 1, "▐", Style(245, bg))
        if not focused:
            return None
        v, c = a.cursor()
        return y + v - a.top, x + 1 + c

    def key(self, name: str) -> bool:
        return self.area.key(name)


class ChoiceField(Field):
    def __init__(self, label: str, options: list[str], index: int = 0, when: Callable[[], bool] | None = None) -> None:
        super().__init__(label, when)
        self.options = options
        self.index = index

    @property
    def value(self) -> str:
        return self.options[self.index]

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        arrow = Style(87, bold=True) if focused else T.FAINT
        text = Style(255, bold=True) if focused else T.TEXT
        parts(cv, y, x, [("◂ ", arrow), (self.value, text), (" ▸", arrow), (f"   {self.index + 1}/{len(self.options)}", T.GHOST)], x + w)
        return None

    def key(self, name: str) -> bool:
        if name in ("left", "h"):
            self.index = (self.index - 1) % len(self.options)
        elif name in ("right", "l", " "):
            self.index = (self.index + 1) % len(self.options)
        else:
            return False
        return True


class ToggleField(Field):
    def __init__(self, label: str, value: bool = False, when: Callable[[], bool] | None = None) -> None:
        super().__init__(label, when)
        self.value = value

    def rows(self, w: int) -> int:
        return max(1, len(wrap(self.label, w - 3)))

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        mark = ("▣", Style(84, bold=True)) if self.value else ("□", T.DIM)
        cv.put(y, x, mark[0], mark[1])
        for i, line in enumerate(wrap(self.label, w - 3)):
            cv.put(y + i, x + 3, line, Style(255, bold=True) if focused else T.TEXT)
        return None

    def key(self, name: str) -> bool:
        if name in (" ", "enter", "x"):
            self.value = not self.value
            return True
        return False


class NoteField(Field):
    focusable = False
    wide = True

    def __init__(self, text: str | Callable[[], str], style: Style = T.DIM, when: Callable[[], bool] | None = None) -> None:
        super().__init__("", when)
        self.text = text
        self.style = style

    def _text(self) -> str:
        return self.text() if callable(self.text) else self.text

    def rows(self, w: int) -> int:
        return max(1, len(wrap(self._text(), w)))

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        for i, line in enumerate(wrap(self._text(), w)):
            cv.put(y + i, x, line, self.style)
        return None


class DocField(Field):
    """Read-only scrolling text inside a form, such as the planner's questions."""

    wide = True

    def __init__(self, text: str, rows: int = 10) -> None:
        super().__init__("")
        self.text = text
        self.n = rows
        self.top = 0
        self._lines: list[str] = []

    def rows(self, w: int) -> int:
        return self.n

    def draw(self, cv: Canvas, y: int, x: int, w: int, focused: bool, t: float) -> tuple[int, int] | None:
        self._lines = wrap(self.text, w - 2)
        self.top = max(0, min(self.top, len(self._lines) - self.n))
        cv.fill(y, x, self.n, w, Style(T.DEFAULT, 233))
        for i, line in enumerate(self._lines[self.top : self.top + self.n]):
            style = Style(87, 233, bold=True) if line[:1].isdigit() else Style(252, 233)
            if line.lstrip().startswith(("Recommended:", "Why it matters:")):
                style = Style(141, 233) if "Recommended" in line else Style(245, 233)
            cv.put(y + i, x + 1, line, style)
        if len(self._lines) > self.n:
            frac = self.top / max(1, len(self._lines) - self.n)
            cv.put(y + int(frac * (self.n - 1)), x + w - 1, "▐", Style(87 if focused else 240, 233))
        return None

    def key(self, name: str) -> bool:
        step = {"up": -1, "down": 1, "pgup": -self.n + 1, "pgdn": self.n - 1}.get(name)
        if step is None:
            return False
        limit = max(0, len(self._lines) - self.n)
        new = max(0, min(limit, self.top + step))
        if new == self.top:
            return False
        self.top = new
        return True


@dataclass
class Button:
    label: str
    action: Callable[[Any, App], None]
    tone: str = "normal"
    hotkey: str | None = None


class Form(Modal):
    def __init__(
        self,
        title: str,
        fields: list[Field],
        buttons: list[Button],
        *,
        width: int = 80,
        accent: int = 51,
        label_w: int = 13,
        submit_hint: str | None = None,
        focus: int = 0,
    ) -> None:
        self.title = title
        self.fields = fields
        self.buttons = buttons
        self.width = width
        self.accent = accent
        self.label_w = label_w
        self.focus = focus
        self.error = ""
        self.submit_hint = submit_hint or buttons[0].label.lower()

    def items(self) -> list[Field | int]:
        out: list[Field | int] = [f for f in self.fields if f.visible() and f.focusable]
        return out + list(range(len(self.buttons)))

    def current(self) -> Field | int:
        items = self.items()
        self.focus = max(0, min(self.focus, len(items) - 1))
        return items[self.focus]

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        scrim(cv)
        w = min(self.width, cv.w - 4)
        fw = w - 4 - self.label_w - 1
        visible = [f for f in self.fields if f.visible()]
        body = sum(f.rows(w - 4 if f.wide else fw) for f in visible)
        spacing = 1 if body + len(visible) * 2 + 8 <= cv.h - 2 else 0
        h = 2 + 1 + body + spacing * max(0, len(visible) - 1) + (2 if self.error else 0) + 4
        y, x, h, w = frame(cv, self.title, w, h, self.accent)
        cur = self.current()
        row = y + 2
        for f in visible:
            fx, fwidth = (x + 2, w - 4) if f.wide else (x + 2 + self.label_w + 1, fw)
            if not f.wide and f.label and not isinstance(f, ToggleField):
                focused = f is cur
                cv.put(row, x + 2, pad(f.label, self.label_w), Style(87, bold=True) if focused else T.DIM)
            cursor = f.draw(cv, row, fx, fwidth, f is cur, t)
            if cursor:
                cv.cursor = cursor
            row += f.rows(fwidth) + spacing
        if self.error:
            for line in wrap(self.error, w - 4)[:1]:
                cv.put(row, x + 2, "✗ " + line, T.ERR)
            row += 2
        by = y + h - 3
        bx = x + 2 + self.label_w + 1
        for i, b in enumerate(self.buttons):
            text, style = button_part(b.label, cur == i, b.tone if i else "primary", self.accent)
            bx = cv.put(by, bx, text, style) + 2
        hints(cv, y + h - 2, x + 2, [("ctrl-s", self.submit_hint), ("tab", "next field"), ("esc", "cancel")], x + w - 2)

    def key(self, name: str, app: App) -> None:
        cur = self.current()
        if name == "esc":
            app.close(self)
            return
        if name == "ctrl-s":
            self.press(0, app)
            return
        if isinstance(cur, Field):
            if name == "ctrl-e" and isinstance(cur, TextField):
                app.edit_text(cur.area.text, cur.area.set)
                return
            if cur.key(name):
                self.error = ""
                return
        else:
            if name in ("left", "right"):
                self.focus += -1 if name == "left" else 1
                return
            if name in ("enter", " "):
                self.press(cur, app)
                return
        for i, b in enumerate(self.buttons):
            if b.hotkey and name == b.hotkey and not isinstance(cur, (LineField, TextField)):
                self.press(i, app)
                return
        n = len(self.items())
        if name in ("tab", "down", "enter"):
            self.focus = (self.focus + 1) % n
        elif name in ("shift-tab", "up"):
            self.focus = (self.focus - 1) % n

    def press(self, index: int, app: App) -> None:
        self.buttons[index].action(self, app)


# ----------------------------------------------------------------- confirm


class Confirm(Modal):
    def __init__(self, title: str, body: str, buttons: list[Button], *, accent: int = 51, width: int = 72) -> None:
        self.title = title
        self.body = body
        self.buttons = buttons
        self.accent = accent
        self.width = width
        self.sel = 0

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        scrim(cv)
        w = min(self.width, cv.w - 4)
        lines = wrap(self.body, w - 6)[: max(1, cv.h - 10)]
        y, x, h, w = frame(cv, self.title, w, len(lines) + 7, self.accent)
        for i, line in enumerate(lines):
            cv.put(y + 2 + i, x + 3, line, T.TEXT if not line.startswith("- ") else T.DIM)
        bx = x + 3
        for i, b in enumerate(self.buttons):
            label = b.label + (f" ({b.hotkey})" if b.hotkey else "")
            text, style = button_part(label, i == self.sel, b.tone if i else ("danger" if b.tone == "danger" else "primary"), self.accent)
            bx = cv.put(y + h - 3, bx, text, style) + 2
        hints(cv, y + h - 2, x + 3, [("enter", "choose"), ("←→", "move"), ("esc", "cancel")], x + w - 2)

    def key(self, name: str, app: App) -> None:
        if name == "esc":
            app.close(self)
        elif name in ("left", "shift-tab", "up"):
            self.sel = (self.sel - 1) % len(self.buttons)
        elif name in ("right", "tab", "down"):
            self.sel = (self.sel + 1) % len(self.buttons)
        elif name in ("enter", " "):
            self.buttons[self.sel].action(self, app)
        else:
            for b in self.buttons:
                if b.hotkey and name == b.hotkey:
                    b.action(self, app)
                    return


# ----------------------------------------------------------------- viewer


@dataclass
class Tab:
    title: str
    load: Callable[[], str]
    path: Path | None = None
    kind: str = "md"
    line: int | None = None


def line_style(line: str, kind: str) -> Style:
    s = line.lstrip()
    if kind == "diff":
        if line.startswith("+") and not line.startswith("+++"):
            return Style(84)
        if line.startswith("-") and not line.startswith("---"):
            return Style(203)
        if line.startswith("@@"):
            return Style(87)
        return T.DIM
    if kind == "log":
        if s.startswith("=== "):
            return Style(214, bold=True)
        if "PAUSED" in s or "error" in s.lower()[:40]:
            return Style(203)
        if s.startswith(("━", "─", "=====")):
            return T.GHOST
        for role, colour in (("[owner]", 214), ("[planner]", 141), ("[supervisor]", 45), ("[bridge]", 110), ("VERDICT:", 45)):
            if s.startswith(role):
                return Style(colour, bold=True)
        return T.TEXT
    if kind == "md":
        if s.startswith("# "):
            return Style(51, bold=True)
        if s.startswith("#"):
            return Style(87, bold=True)
        if s.startswith("```"):
            return T.FAINT
        if s.startswith("|"):
            return Style(250)
        if s.startswith(("- Status:", "Status:")):
            return Style(214) if "AUTONOMOUS" in s or "AWAITING" in s or "OWNER-BLOCKED" in s else Style(84)
        if s.startswith(("- Decided by:", "> ")):
            return T.DIM
    return T.TEXT


class Viewer(Modal):
    full = True

    def __init__(
        self,
        tabs: list[Tab],
        *,
        title: str = "",
        index: int = 0,
        actions: list[tuple[str, str, Callable[[Viewer, App], None]]] | None = None,
        wrap_on: bool = True,
    ) -> None:
        self.tabs = tabs
        self.title = title
        self.index = index
        self.actions = actions or []
        self.wrap_on = wrap_on
        self._text: dict[int, list[str]] = {}
        self._rows: dict[tuple[int, int, bool], list[tuple[int, str]]] = {}
        self.top = 0
        self.search: LineEdit | None = None
        self.query = ""
        self._jump = tabs[index].line if tabs else None
        self.rows_h = 10
        self.body_w = 60

    def reload(self) -> None:
        self._text.clear()
        self._rows.clear()

    def lines(self, i: int | None = None) -> list[str]:
        i = self.index if i is None else i
        if i not in self._text:
            try:
                text = self.tabs[i].load()
            except OSError as e:
                text = f"(cannot read: {e})"
            self._text[i] = (text or "(empty)").replace("\t", "    ").split("\n")
        return self._text[i]

    def visual(self, w: int) -> list[tuple[int, str]]:
        key = (self.index, w, self.wrap_on)
        if key not in self._rows:
            rows: list[tuple[int, str]] = []
            for n, line in enumerate(self.lines()):
                if self.wrap_on and width(line) > w:
                    for j, chunk in enumerate(wrap(line, w) or [""]):
                        rows.append((n if j == 0 else -1, chunk))
                else:
                    rows.append((n, line))
            self._rows[key] = rows
        return self._rows[key]

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        y, x, h, w = 1, 1, cv.h - 2, cv.w - 2
        cv.shadow(y, x, h, w)
        cv.panel(y, x, h, w, Style(51), T.PANEL_BG, title=f" {self.title} " if self.title else None, title_style=T.ACCENT)
        tx = x + 2
        for i, tab in enumerate(self.tabs):
            label = f" {tab.title} "
            style = Style(16, 51, bold=True) if i == self.index else Style(250, 237)
            tx = cv.put(y + 1, tx, label, style, x + w - 2) + 1
        num_w = 6
        body_w = self.body_w = w - 4 - num_w
        rows = self.visual(body_w)
        self.rows_h = h - 5
        if self._jump is not None:
            target = next((k for k, (n, _) in enumerate(rows) if n == self._jump - 1), 0)
            self.top = max(0, target - self.rows_h // 3)
            self._jump = None
        self.top = max(0, min(self.top, max(0, len(rows) - self.rows_h)))
        kind = self.tabs[self.index].kind if self.tabs else "text"
        for k, (n, text) in enumerate(rows[self.top : self.top + self.rows_h]):
            yy = y + 3 + k
            if n >= 0:
                cv.put(yy, x + 2, f"{n + 1:>5} ", T.GHOST)
            style = line_style(self.lines()[n] if n >= 0 else text, kind)
            if self.query and self.query.lower() in text.lower():
                cv.fill(yy, x + 2 + num_w, 1, body_w, Style(T.DEFAULT, 58))
            cv.put(yy, x + 2 + num_w, text, style, x + w - 2)
        fy = y + h - 2
        if self.search is not None:
            view, cur = self.search.view(w - 8)
            parts(cv, fy, x + 2, [("/ ", Style(87, bold=True)), (view, Style(255))])
            cv.cursor = (fy, x + 4 + cur)
            return
        pos = f"{min(len(rows), self.top + self.rows_h)}/{len(rows)}"
        items = [("↑↓", "scroll"), ("tab", "switch"), ("/", "search"), ("w", "wrap")]
        if self.tabs and self.tabs[self.index].path:
            items.append(("e", "edit"))
        items += [(k, label) for k, label, _ in self.actions] + [("esc", "close")]
        hints(cv, fy, x + 2, items, x + w - 2 - len(pos) - 2)
        cv.put(fy, x + w - 2 - len(pos), pos, T.FAINT)

    def key(self, name: str, app: App) -> None:
        if self.search is not None:
            if name == "esc":
                self.search = None
            elif name == "enter":
                self.query = self.search.text
                self.search = None
                self.find(1)
            else:
                self.search.key(name)
            return
        for k, _, fn in self.actions:
            if name == k:
                fn(self, app)
                return
        page = max(1, self.rows_h - 1)
        if name in ("esc", "q"):
            app.close(self)
        elif name in ("down", "j"):
            self.top += 1
        elif name in ("up", "k"):
            self.top = max(0, self.top - 1)
        elif name in ("pgdn", " "):
            self.top += page
        elif name in ("pgup", "b"):
            self.top = max(0, self.top - page)
        elif name in ("home", "g"):
            self.top = 0
        elif name in ("end", "G"):
            self.top = 1 << 30
        elif name in ("tab", "right", "l") and len(self.tabs) > 1:
            self.index = (self.index + 1) % len(self.tabs)
            self.top = 0
        elif name in ("shift-tab", "left", "h") and len(self.tabs) > 1:
            self.index = (self.index - 1) % len(self.tabs)
            self.top = 0
        elif name == "/":
            self.search = LineEdit(self.query)
        elif name == "n":
            self.find(1)
        elif name == "N":
            self.find(-1)
        elif name == "w":
            self.wrap_on = not self.wrap_on
        elif name == "e" and self.tabs and self.tabs[self.index].path:
            app.edit_file(self.tabs[self.index].path, self.reload)

    def find(self, step: int) -> None:
        if not self.query:
            return
        rows = self.visual(self.body_w)
        q = self.query.lower()
        n = len(rows)
        for k in range(1, n + 1):
            i = (self.top + 2 + step * k) % n
            if q in rows[i][1].lower():
                self.top = max(0, i - 2)
                return


# ----------------------------------------------------------------- palette and lists


@dataclass
class Command:
    label: str
    key: str
    run: Callable[[App], None]
    why_not: Callable[[], str | None] = lambda: None
    group: str = ""


class Palette(Modal):
    def __init__(self, commands: list[Command]) -> None:
        self.commands = commands
        self.edit = LineEdit(placeholder="type to filter, ⏎ to run")
        self.list = ListState()
        self.list.index = next((i for i, c in enumerate(commands) if c.why_not() is None), 0)

    def matches(self) -> list[Command]:
        words = self.edit.text.lower().split()
        return [c for c in self.commands if all(w in f"{c.label} {c.group} {c.key}".lower() for w in words)]

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        scrim(cv)
        found = self.matches()
        rows = min(len(found), max(3, cv.h - 12), 16)
        y, x, h, w = frame(cv, "COMMANDS", 74, rows + 7, 141)
        cv.fill(y + 2, x + 2, 1, w - 4, Style(T.DEFAULT, 239))
        view, cur = self.edit.view(w - 8)
        cv.put(y + 2, x + 3, "› ", Style(141, 239, bold=True))
        cv.put(y + 2, x + 5, view or self.edit.placeholder, Style(255 if view else 243, 239))
        cv.cursor = (y + 2, x + 5 + cur)
        self.list.clamp(len(found), rows)
        for i, c in enumerate(found[self.list.top : self.list.top + rows]):
            yy = y + 4 + i
            sel = self.list.top + i == self.list.index
            reason = c.why_not()
            if sel:
                cv.fill(yy, x + 1, 1, w - 2, Style(T.DEFAULT, 238))
                cv.put(yy, x + 1, "▌", Style(141, 238, bold=True))
            style = (Style(255, bold=True) if sel else T.TEXT) if reason is None else T.FAINT
            cv.put(yy, x + 3, clip(c.label, w - 16), style)
            if c.key:
                cv.put(yy, x + w - 3 - width(c.key) - 2, f" {c.key} ", Style(87, 237, bold=True) if reason is None else T.KEY_OFF)
        sel_cmd = found[self.list.index] if found else None
        why = sel_cmd.why_not() if sel_cmd else None
        if why:
            cv.put(y + h - 2, x + 3, clip("✗ " + why, w - 6), T.WARN)
        else:
            hints(cv, y + h - 2, x + 3, [("⏎", "run"), ("↑↓", "choose"), ("esc", "close")], x + w - 2)

    def key(self, name: str, app: App) -> None:
        found = self.matches()
        if name == "esc":
            app.close(self)
        elif name in ("up", "down", "pgup", "pgdn"):
            self.list.key(name, len(found))
        elif name == "enter":
            if not found:
                return
            c = found[self.list.index]
            reason = c.why_not()
            if reason:
                app.toast(reason, "warn")
                return
            app.close(self)
            c.run(app)
        elif self.edit.key(name):
            found = self.matches()
            self.list.index = next((i for i, c in enumerate(found) if c.why_not() is None), 0)


class Picker(Modal):
    """A list of rows with a detail pane and per-row key actions (plan changes, the owner to-do)."""

    def __init__(
        self,
        title: str,
        rows: list[tuple[str, str, Style]],
        detail: Callable[[int], str],
        actions: list[tuple[str, str, Callable[[int, App], None]]],
        *,
        accent: int = 214,
        empty: str = "Nothing here.",
    ) -> None:
        self.title = title
        self.rows = rows
        self.detail = detail
        self.actions = actions
        self.accent = accent
        self.empty = empty
        self.list = ListState()

    def draw(self, cv: Canvas, app: App, t: float) -> None:
        scrim(cv)
        w = min(110, cv.w - 4)
        h = min(cv.h - 2, max(14, len(self.rows) + 12))
        y, x, h, w = frame(cv, self.title, w, h, self.accent)
        list_h = max(3, min(len(self.rows), (h - 6) // 2))
        if not self.rows:
            cv.put(y + 2, x + 3, self.empty, T.DIM)
        self.list.clamp(len(self.rows), list_h)
        for i, (left, right, style) in enumerate(self.rows[self.list.top : self.list.top + list_h]):
            yy = y + 2 + i
            sel = self.list.top + i == self.list.index
            if sel:
                cv.fill(yy, x + 1, 1, w - 2, Style(T.DEFAULT, 238))
                cv.put(yy, x + 1, "▌", Style(self.accent, 238, bold=True))
            end = cv.put(yy, x + 3, clip(left, w - 30), style.b() if sel else style)
            cv.put(yy, max(end + 2, x + w - 3 - width(right)), clip(right, 26), T.FAINT)
        dy = y + 3 + list_h
        cv.hline(dy - 1, x + 2, w - 4, T.GHOST)
        if self.rows:
            for i, line in enumerate(wrap(self.detail(self.list.index), w - 6)[: h - (dy - y) - 3]):
                cv.put(dy + i, x + 3, line, T.TEXT)
        hints(cv, y + h - 2, x + 3, [(k, label) for k, label, _ in self.actions] + [("↑↓", "choose"), ("esc", "close")], x + w - 2)

    def key(self, name: str, app: App) -> None:
        if name in ("esc", "q"):
            app.close(self)
            return
        if self.list.key(name, len(self.rows)):
            return
        for k, _, fn in self.actions:
            if name == k and self.rows:
                fn(self.list.index, app)
                return


def todo_detail(item: Any, repo: Path) -> str:
    where = item.where(repo)
    return f"{item.ident}: {one_line(item.title)}\n\n{('Decided by: ' + item.detail) if item.detail else ''}\n{where}"
