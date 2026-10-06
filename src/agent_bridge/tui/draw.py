"""Drawing the screens onto a Canvas. Pure functions of the snapshot and the UI state."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from agent_bridge.tui import theme as T
from agent_bridge.tui.canvas import Canvas
from agent_bridge.tui.model import STATUS_LABEL, Line, ProjectRow, RoleView, Snapshot, next_step, short_model
from agent_bridge.tui.text import ago, clip, clock, one_line, pad, tilde, tokens, width, wrap
from agent_bridge.tui.theme import Style

Part = tuple[str, Style]

PIXELS = {
    "A": (".##.", "#..#", "####", "#..#", "#..#"),
    "G": (".###", "#...", "#.##", "#..#", ".###"),
    "E": ("####", "#...", "###.", "#...", "####"),
    "N": ("#..#", "##.#", "#.##", "#..#", "#..#"),
    "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "B": ("###.", "#..#", "###.", "#..#", "###."),
    "R": ("###.", "#..#", "###.", "#.#.", "#..#"),
    "I": ("#", "#", "#", "#", "#"),
    "D": ("###.", "#..#", "#..#", "#..#", "###."),
    " ": ("..", "..", "..", "..", ".."),
}


def _glyph(ch: str) -> tuple[str, str, str]:
    """Five pixel rows folded into three rows of half blocks."""
    rows = [*PIXELS.get(ch, PIXELS[" "]), "." * len(PIXELS.get(ch, PIXELS[" "])[0])]
    out = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        out.append("".join("█" if t == b == "#" else "▀" if t == "#" else "▄" if b == "#" else " " for t, b in zip(top, bottom)))
    return out[0], out[1], out[2]


def logo_rows(word: str = "AGENT BRIDGE") -> list[str]:
    rows = ["", "", ""]
    for i, ch in enumerate(word):
        glyph = _glyph(ch)
        gap = " " if i + 1 < len(word) else ""
        for r in range(3):
            rows[r] += glyph[r] + gap
    return rows
ENGINE_LABEL = {"claude-code": "Claude Code", "opencode": "opencode", "fake": "fake"}
TONE = {"info": T.ACCENT_SOFT, "warn": T.WARN, "bad": T.ERR, "good": T.OK}
_KEY = re.compile(r"«([^»]+)»")


@dataclass
class View:
    """UI state the drawing needs besides the snapshot."""

    t: float = 0.0
    scroll: int = 0
    wrap: bool = False
    jobs: list[str] = field(default_factory=list)
    toasts: list[tuple[str, str]] = field(default_factory=list)
    starting: bool = False
    selected: int = 0


# ----------------------------------------------------------------- small pieces


def parts(cv: Canvas, y: int, x: int, items: Iterable[Part], max_x: int | None = None) -> int:
    for text, style in items:
        x = cv.put(y, x, text, style, max_x)
    return x


def keycap(key: str, on: bool = True) -> Part:
    return (f" {key} ", Style(16, 87, bold=True) if on else T.KEY_OFF)


def hint_parts(text: str, tone: Style) -> list[Part]:
    out: list[Part] = []
    pos = 0
    for m in _KEY.finditer(text):
        if m.start() > pos:
            out.append((text[pos : m.start()], tone))
        out.append((f" {m.group(1)} ", Style(87, 237, bold=True)))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], tone))
    return out


def gradient_text(cv: Canvas, y: int, x: int, text: str, t: float, *, speed: float = 6.0, bold: bool = True) -> int:
    shift = int(t * speed)
    for i, ch in enumerate(text):
        colour = T.GRADIENT[(i + shift) % len(T.GRADIENT)]
        x = cv.put(y, x, ch, Style(colour, bold=bold))
    return x


def chip(status: str, t: float) -> list[Part]:
    fg, bg = T.STATE_CHIP.get(status, (16, 245))
    live = status in ("running", "planning")
    dot = "●" if not live or T.pulse(t, 1.2) > 0.35 else "○"
    return [(f" {dot} {STATUS_LABEL.get(status, status.upper())} ", Style(fg, bg, bold=True))]


def big_logo(cv: Canvas, y: int, x: int, t: float, word: str = "AGENT BRIDGE") -> int:
    rows = logo_rows(word)
    shift = int(t * 8)
    for r, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch != " ":
                cv.put(y + r, x + i, ch, Style(T.GRADIENT[(i // 2 + shift) % len(T.GRADIENT)], bold=True))
    return width(rows[0])


def logo_width(word: str = "AGENT BRIDGE") -> int:
    return width(logo_rows(word)[0])


def scanline(cv: Canvas, y: int, t: float, active: bool) -> None:
    """A hairline under the header; while the bridge runs, a brighter segment sweeps across it."""
    w = cv.w
    head = int((t * 40) % (w + 24)) - 12 if active else -100
    for x in range(w):
        d = abs(x - head)
        if d < 12:
            colour = T.ramp("cyan", 1 - d / 12)
        else:
            colour = 236
        cv.put(y, x, "▔", Style(colour, T.CANVAS_BG))


# ----------------------------------------------------------------- header and footer


def header(cv: Canvas, snap: Snapshot | None, view: View, title: str | None = None) -> None:
    cv.fill(0, 0, 1, cv.w, Style(T.DEFAULT, 234))
    x = cv.put(0, 1, "◢◤", Style(T.GRADIENT[int(view.t * 6) % len(T.GRADIENT)], bold=True))
    x = gradient_text(cv, 0, x + 1, "AGENT·BRIDGE", view.t)
    right: list[Part] = []
    if snap is not None and title is None:
        right = chip(snap.status, view.t)
        st = snap.state
        if snap.configured:
            right.append((f"  exchange {st.exchange}", T.DIM))
        if snap.holder:
            right.append((f"  pid {snap.holder.get('pid')}", T.FAINT))
    right.append((f"  {datetime.now():%H:%M:%S} ", T.FAINT))
    rw = sum(width(p[0]) for p in right)
    limit = cv.w - rw - 2
    x = cv.put(0, x + 1, "│", T.GHOST, limit)
    if title is not None:
        cv.put(0, x + 1, title, T.BRIGHT.b(), limit)
    elif snap is not None:
        x = cv.put(0, x + 1, snap.name, T.BRIGHT.b(), limit)
        cv.put(0, x + 2, tilde(snap.root), T.FAINT, limit)
    parts(cv, 0, cv.w - rw, right)


def footer(cv: Canvas, items: list[tuple[str, str, bool]], view: View) -> None:
    y = cv.h - 1
    cv.fill(y, 0, 1, cv.w, Style(T.DEFAULT, 234))
    right: list[Part] = []
    if view.jobs:
        right = [(f" {T.spinner(view.t)} ", Style(87, bold=True)), (clip(", ".join(view.jobs), 30) + " ", T.DIM)]
    rw = sum(width(p[0]) for p in right)
    x = 1
    for key, label, on in items:
        need = width(key) + 2 + width(label) + 2
        if x + need > cv.w - rw - 1:
            break
        x = parts(cv, y, x, [keycap(key, on), (f" {label}  ", T.KEY_LABEL if on else T.KEY_LABEL_OFF)])
    if right:
        parts(cv, y, cv.w - rw, right)


def toasts(cv: Canvas, items: list[tuple[str, str]]) -> None:
    y = cv.h - 2
    for text, tone in reversed(items[-3:]):
        colour = TONE.get(tone, T.ACCENT_SOFT).fg
        body = clip(one_line(text), max(10, min(70, cv.w - 8)))
        w = width(body) + 4
        x = cv.w - w - 1
        cv.fill(y, x, 1, w, Style(T.DEFAULT, 236))
        cv.put(y, x, "▌", Style(colour, 236))
        cv.put(y, x + 2, body, Style(255, 236))
        y -= 1
        if y < 2:
            break


# ----------------------------------------------------------------- role pods


def _read_only_part(rv: RoleView) -> Part:
    if rv.read_only == "writes":
        return ("✎ writes the repo", Style(T.ROLE_DIM[rv.role] if rv.role in T.ROLE_DIM else 245))
    if rv.read_only.startswith("enforced"):
        return ("◆ read-only · enforced", T.OK)
    return ("◇ read-only · by instruction only", T.WARN)


def gauge(cv: Canvas, y: int, x: int, w: int, value: int | None, limit: int) -> None:
    label = f" {tokens(value)}/{tokens(limit)}"
    bar_w = max(4, w - 4 - width(label))
    frac = min(1.0, (value or 0) / limit) if limit else 0.0
    filled = int(round(frac * bar_w))
    colour = 84 if frac < 0.5 else 214 if frac < 0.8 else 203
    x = cv.put(y, x, "ctx ", T.FAINT)
    x = cv.put(y, x, "━" * filled, Style(colour))
    x = cv.put(y, x, "─" * (bar_w - filled), T.GHOST)
    cv.put(y, x, label, T.DIM)


def pod(cv: Canvas, y: int, x: int, h: int, w: int, rv: RoleView, view: View) -> None:
    role = rv.role
    colour = T.ROLE[role]
    if rv.active:
        border = Style(T.ramp(T.ROLE_RAMP[role], 0.45 + 0.55 * T.pulse(view.t)), bold=True)
    else:
        border = Style(T.ROLE_DIM[role])
    title = f" {T.GLYPH[role]} {role.upper()} "
    cv.panel(y, x, h, w, border, T.PANEL_BG, title=title, title_style=Style(colour, bold=True),
             right=" LIVE " if rv.active else None, right_style=Style(16, colour, bold=True))
    iw = w - 4
    ix = x + 2
    rows: list[list[Part]] = []
    model = [(ENGINE_LABEL.get(rv.engine, rv.engine), T.DIM), ("  ", T.DIM), (short_model(rv.model), T.BRIGHT.b())]
    if rv.variant:
        model.append((f"  {rv.variant}", Style(colour)))
    rows.append(model)
    rows.append([_read_only_part(rv)])
    if rv.active:
        elapsed = clock((datetime.now(rv.since.tzinfo) - rv.since).total_seconds()) if rv.since else ""
        rows.append([(T.spinner(view.t) + " ", Style(colour, bold=True)), ("working ", Style(colour, bold=True)), (elapsed, T.BRIGHT)])
        rows.append([("› ", Style(T.ROLE_DIM[role])), (one_line(rv.doing) or "…", T.DIM)])
    else:
        rows.append([("○ standby", T.FAINT)])
        rows.append([(f"session {rv.session[:8]}" if rv.session else "no session yet", T.FAINT)])
    inner = h - 2
    if inner <= 3:
        rows = [rows[0], rows[2]]
    for i, row in enumerate(rows[: max(0, inner - 1)]):
        parts(cv, y + 1 + i, ix, row, ix + iw)
    if inner >= 2:
        gauge(cv, y + h - 2, ix, iw, rv.context, rv.limit)


def flow(cv: Canvas, y: int, centres: dict[str, int], snap: Snapshot, view: View) -> None:
    left, right = min(centres.values()), max(centres.values())
    cv.put(y, left, "┄" * (right - left + 1), T.GHOST)
    for role, cx in centres.items():
        cv.put(y, cx, "┴", Style(T.ROLE_DIM[role]))
    a = snap.active
    if not a or not snap.holder:
        return
    src = {"builder": "supervisor", "supervisor": "builder", "planner": "supervisor"}[a.role]
    s, d = centres[src], centres[a.role]
    frac = (view.t * 0.7) % 1.0
    pos = int(s + (d - s) * frac)
    step = 1 if d > s else -1
    colour = T.ROLE[a.role]
    for k in range(1, 6):
        tx = pos - step * k
        if min(s, d) <= tx <= max(s, d):
            cv.put(y, tx, "━", Style(T.ramp(T.ROLE_RAMP[a.role], 1 - k / 6)))
    cv.put(y, pos, "●", Style(colour, bold=True))


# ----------------------------------------------------------------- stream


def line_parts(line: Line) -> list[Part]:
    role = line.role if line.role in T.ROLE else "bridge"
    rc, rd = T.ROLE[role], T.ROLE_DIM[role]
    k = line.kind
    if k == "turn":
        return [("▶ ", Style(rc, bold=True)), (line.text, Style(rc, bold=True)), ("  " + line.extra, T.FAINT)]
    if k == "tool":
        return [("› ", Style(rd)), (line.text, Style(rc)), ("  " + line.extra, T.DIM)]
    if k == "text":
        return [(line.text, T.TEXT)]
    if k == "done":
        return [("✓ ", T.OK), (line.text, T.DIM)]
    if k == "report":
        return [("⬢ ", Style(rc)), (line.text, T.DIM)]
    if k == "verdict":
        out = [("◆ VERDICT ", Style(T.ROLE["supervisor"], bold=True)), (line.text, T.BRIGHT)]
        return out + ([(f"  scope {line.extra}", T.FAINT)] if line.extra else [])
    if k == "good":
        return [("✓ ", T.OK.b()), (line.text, T.OK.b()), ("  " + line.extra, T.FAINT)]
    if k == "bad":
        return [("✗ ", T.ERR.b()), (line.text, T.ERR.b()), ("  " + line.extra, T.DIM)]
    if k == "alert":
        return [("▲ ", T.WARN), (line.text, T.WARN), ("  " + line.extra, T.DIM)]
    if k == "wait":
        return [("◷ ", T.INFO), (line.text, T.INFO), ("  " + line.extra, T.FAINT)]
    if k == "message":
        return [("» ", Style(rc, bold=True)), (line.text, Style(rc)), (f"  {line.extra}" if line.extra else "", T.FAINT)]
    if k == "ask":
        return [("? ", Style(rc, bold=True)), (line.text, Style(rc))]
    if k == "change":
        return [("Δ ", Style(rc, bold=True)), (line.text, Style(rc)), ("  " + line.extra, T.FAINT)]
    if k == "launch":
        return [("━━ ", T.GHOST), (line.text + " ", T.FAINT), ("━" * 200, T.GHOST)]
    return [("· ", T.FAINT), (line.text, T.DIM), ("  " + line.extra if line.extra else "", T.FAINT)]


def _stamp(line: Line) -> str:
    try:
        return datetime.fromisoformat(line.ts).strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"


def stream_rows(lines: list[Line], w: int, wrap_on: bool) -> list[list[Part]]:
    out: list[list[Part]] = []
    badge_w = 13
    body_w = max(10, w - 9 - badge_w)
    for line in lines:
        role = line.role if line.role in T.ROLE else "bridge"
        head: list[Part] = [(_stamp(line) + " ", T.FAINT)]
        if line.kind == "launch":
            out.append(head + line_parts(line))
            continue
        badge = pad(f"{T.GLYPH[role]} {role}", badge_w - 1) + " "
        head.append((badge, Style(T.ROLE[role])))
        body = line_parts(line)
        if not wrap_on:
            out.append(head + body)
            continue
        text = "".join(p[0] for p in body)
        style = next((p[1] for p in body if p[0].strip()), T.TEXT)
        chunks = wrap(text, body_w) or [""]
        out.append(head + [(chunks[0], style)])
        indent = " " * (9 + badge_w)
        out.extend([[(indent, T.FAINT), (c, style)] for c in chunks[1:]])
    return out


def stream(cv: Canvas, y: int, x: int, h: int, w: int, snap: Snapshot, view: View, focused: bool) -> None:
    live = view.scroll == 0
    right = (" ● LIVE " if snap.holder else " ◦ LATEST ") if live else f" ‖ {view.scroll} back · end to follow "
    cv.panel(y, x, h, w, Style(240 if focused else 238), T.PANEL_BG, title=" STREAM ", title_style=T.ACCENT,
             right=right, right_style=T.OK.b() if live else T.WARN.b())
    inner_h, inner_w = h - 2, w - 4
    rows = stream_rows(snap.lines, inner_w, view.wrap)
    if not rows:
        msg = "Nothing has happened here yet." if snap.configured else ""
        cv.put(y + h // 2, x + max(2, (w - width(msg)) // 2), msg, T.FAINT)
        return
    view.scroll = max(0, min(view.scroll, max(0, len(rows) - inner_h)))
    end = len(rows) - view.scroll
    shown = rows[max(0, end - inner_h) : end]
    for i, row in enumerate(shown):
        parts(cv, y + 1 + i, x + 2, row, x + w - 2)


# ----------------------------------------------------------------- side cards


def owner_card(cv: Canvas, y: int, x: int, h: int, w: int, snap: Snapshot) -> None:
    cv.panel(y, x, h, w, Style(238), T.PANEL_BG, title=" OWNER ", title_style=Style(T.ROLE["owner"], bold=True))
    todo = snap.todo
    rows: list[list[Part]] = []
    n_dec, n_blk = len(todo.get("decisions", [])), len(todo.get("blocked", []))
    waiting = snap.waiting_changes
    asked = (snap.state.review or {}).get("decisions") or []
    if waiting:
        rows.append([("Δ ", T.WARN.b()), (f"{len(waiting)} plan change{'s' * (len(waiting) != 1)} waiting ", T.WARN), (" ".join(c.get("id", "") for c in waiting), T.DIM)])
    if asked:
        rows.append([("? ", Style(T.ROLE["builder"], bold=True)), (f"{len(asked)} builder question{'s' * (len(asked) != 1)}", T.TEXT)])
    if n_dec:
        rows.append([("◆ ", Style(T.ROLE["owner"])), (f"{n_dec} decision{'s' * (n_dec != 1)} to review", T.TEXT)])
    if n_blk:
        rows.append([("▲ ", T.WARN), (f"{n_blk} OWNER-BLOCKED item{'s' * (n_blk != 1)}", T.TEXT)])
    if snap.drift:
        rows.append([("≠ ", T.ERR), ("contract changed: " + ", ".join(snap.drift), T.ERR)])
    if not rows:
        rows.append([("✓ ", T.OK), ("nothing waits for you", T.DIM)])
    for i, row in enumerate(rows[: h - 3]):
        parts(cv, y + 1 + i, x + 2, row, x + w - 2)
    keys = [("o", "to-do"), ("a", "approve")] if waiting or snap.drift else [("o", "to-do"), ("d", "decide")]
    out: list[Part] = []
    for k, label in keys:
        out += [(f" {k} ", Style(87, 237, bold=True)), (f" {label}   ", T.FAINT)]
    parts(cv, y + h - 2, x + 2, out, x + w - 2)


def phases_card(cv: Canvas, y: int, x: int, h: int, w: int, snap: Snapshot, view: View) -> None:
    cv.panel(y, x, h, w, Style(238), T.PANEL_BG, title=" PHASES ", title_style=T.ACCENT)
    track = snap.phases
    if not track:
        note = f"see {snap.cfg.project.phases}" if snap.cfg and snap.cfg.project.phases else "no Phase headings in the PRD"
        cv.put(y + 1, x + 2, clip(note, w - 4), T.FAINT)
        return
    room = h - 2
    current = next((i for i, (_, s) in enumerate(track) if s == "current"), len(track) - 1)
    start = max(0, min(current - room // 2, len(track) - room))
    for i, (title, status) in enumerate(track[start : start + room]):
        if status == "done":
            mark, style = ("✓", T.OK), T.DIM
        elif status == "current":
            mark, style = ("▶", Style(T.ramp("cyan", 0.5 + 0.5 * T.pulse(view.t)), bold=True)), T.BRIGHT.b()
        elif status == "paused":
            mark, style = ("‖", T.WARN), T.WARN
        else:
            mark, style = ("·", T.FAINT), T.FAINT
        parts(cv, y + 1 + i, x + 2, [(mark[0] + " ", mark[1]), (title, style)], x + w - 2)


def alerts_card(cv: Canvas, y: int, x: int, h: int, w: int, snap: Snapshot) -> None:
    cv.panel(y, x, h, w, Style(238), T.PANEL_BG, title=" ALERTS ", title_style=T.WARN.b(), right=f" {len(snap.warnings)} " if snap.warnings else None, right_style=T.DIM)
    items = snap.warnings[-(h - 2) :] if h > 2 else []
    if not items:
        cv.put(y + 1, x + 2, "✓ none since the last launch", T.FAINT)
        return
    from agent_bridge.tui.model import _BAD, _GOOD

    for i, (ts, title) in enumerate(items):
        tone = T.OK if _GOOD.search(title) else T.ERR if _BAD.search(title) else T.WARN
        parts(cv, y + 1 + i, x + 2, [(ts[11:16] + " ", T.FAINT), (title, tone)], x + w - 2)


# ----------------------------------------------------------------- screens


def dashboard(cv: Canvas, snap: Snapshot, view: View, focused: str = "stream") -> None:
    W, H = cv.w, cv.h
    header(cv, snap, view)
    scanline(cv, 1, view.t, bool(snap.holder))
    y = 2
    full = W >= 96 and H >= 30
    pod_h = 7 if full else 5 if H >= 22 and W >= 72 else 0
    if pod_h:
        gap = 1
        pw = (W - 2 - 2 * gap) // 3
        centres = {}
        for i, role in enumerate(("planner", "supervisor", "builder")):
            px = 1 + i * (pw + gap)
            w = pw if i < 2 else W - 1 - px
            if role in snap.roles:
                pod(cv, y, px, pod_h, w, snap.roles[role], view)
            centres[role] = px + w // 2
        y += pod_h
        if full:
            flow(cv, y, centres, snap, view)
            y += 1
    else:
        x = 1
        for role in ("planner", "supervisor", "builder"):
            rv = snap.roles.get(role)
            if not rv:
                continue
            mark = T.spinner(view.t) if rv.active else T.GLYPH[role]
            x = parts(cv, y, x, [(mark + " ", Style(T.ROLE[role], bold=True)), (short_model(rv.model) + "   ", T.DIM if not rv.active else T.BRIGHT)])
        y += 1
    text, tone = next_step(snap, view.starting)
    cv.fill(y, 0, 1, W, Style(T.DEFAULT, T.CANVAS_BG))
    parts(cv, y, 1, [("▸ ", TONE[tone].b())] + hint_parts(text, TONE[tone]), W - 1)
    y += 1
    main_h = H - 1 - y
    side_w = max(34, W * 34 // 100) if W >= 100 else 0
    stream_w = W - 2 - (side_w + 1 if side_w else 0)
    stream(cv, y, 1, main_h, stream_w, snap, view, focused == "stream")
    if side_w:
        sx = 1 + stream_w + 1
        owner_h = 7
        phase_h = max(4, min(len(snap.phases) + 2, 9)) if snap.phases else 4
        alerts_h = main_h - owner_h - phase_h
        if alerts_h < 4:
            phase_h = max(3, main_h - owner_h - 4)
            alerts_h = main_h - owner_h - phase_h
        owner_card(cv, y, sx, owner_h, side_w, snap)
        phases_card(cv, y + owner_h, sx, phase_h, side_w, snap, view)
        if alerts_h >= 3:
            alerts_card(cv, y + owner_h + phase_h, sx, alerts_h, side_w, snap)


def welcome(cv: Canvas, snap: Snapshot, view: View, options: list[tuple[str, str, str, bool]]) -> None:
    W, H = cv.w, cv.h
    header(cv, snap, view)
    scanline(cv, 1, view.t, False)
    lw = logo_width()
    top = max(3, H // 2 - 9)
    if W >= lw + 4:
        big_logo(cv, top, (W - lw) // 2, view.t)
        top += 4
    tag = [("planner ", Style(T.ROLE["planner"])), ("◇  ", T.GHOST), ("supervisor ", Style(T.ROLE["supervisor"])), ("◆  ", T.GHOST), ("builder ", Style(T.ROLE["builder"])), ("◈", T.GHOST)]
    tw = sum(width(p[0]) for p in tag)
    parts(cv, top, (W - tw) // 2, tag)
    top += 2
    cw = min(76, W - 4)
    cx = (W - cw) // 2
    ch = len(options) * 2 + 5
    cv.shadow(top, cx, ch, cw)
    cv.panel(top, cx, ch, cw, Style(239), T.PANEL_BG, title=" GET STARTED ", title_style=T.ACCENT)
    what = f"No agent-bridge project in {tilde(snap.root)}" if not snap.error else f"bridge.toml in {tilde(snap.root)} has a problem"
    cv.put(top + 1, cx + 3, clip(what, cw - 6), T.BRIGHT.b())
    for i, (key, label, detail, on) in enumerate(options):
        yy = top + 3 + i * 2
        sel = i == view.selected
        if sel:
            cv.tint(yy, cx + 1, 1, cw - 2, T.SELECT_BG)
            cv.put(yy, cx + 1, "▌", Style(87, T.SELECT_BG, bold=True))
        x = parts(cv, yy, cx + 3, [keycap(key, on), ("  " + label, (T.BRIGHT.b() if on else T.FAINT))], cx + cw - 2)
        cv.put(yy, x + 2, clip(detail, cx + cw - 4 - x), T.DIM if on else T.GHOST)
    if snap.error:
        for i, line in enumerate(wrap(snap.error, cw - 6)[:3]):
            cv.put(top + ch + 1 + i, cx + 3, line, T.ERR)


def projects(cv: Canvas, rows: list[ProjectRow], view: View, selected: int, top: int) -> int:
    """Draws the all-projects list; returns how many rows fit, for scrolling."""
    W, H = cv.w, cv.h
    running = sum(1 for r in rows if r.holder)
    header(cv, None, view, title=f"ALL PROJECTS  ·  {len(rows)} known  ·  {running} running")
    scanline(cv, 1, view.t, running > 0)
    y = 3
    cols = [("STATE", 17), ("PROJECT", 22), ("EXCH", 6), ("ACTIVE", 10), ("LAST VERDICT", 0)]
    path_w = max(18, W // 4)
    x = 3
    for name, w in cols:
        cv.put(y, x, name, T.FAINT.b())
        x += w if w else 0
    cv.put(y, W - path_w - 2, "PATH", T.FAINT.b())
    cv.hline(y + 1, 2, W - 4, T.GHOST)
    room = H - y - 4
    if not rows:
        cv.put(y + 3, 3, "No projects yet. Press n to plan one; every repo agent-bridge opens is listed here.", T.DIM)
        return room
    for i, r in enumerate(rows[top : top + room]):
        yy = y + 2 + i
        sel = top + i == selected
        if sel:
            cv.tint(yy, 1, 1, W - 2, T.SELECT_BG)
            cv.put(yy, 1, "▌", Style(87, T.SELECT_BG, bold=True))
        x = 3
        fg, bg = T.STATE_CHIP.get(r.status, (16, 245))
        label = "MISSING" if r.missing else STATUS_LABEL.get(r.status, r.status.upper())
        cv.put(yy, x, pad(f" {label} ", 15), Style(fg, bg, bold=True) if not r.missing else Style(16, 240, bold=True))
        x += 17
        name = r.name + ("  (here)" if r.here else "")
        cv.put(yy, x, pad(name, 21), T.BRIGHT.b() if sel else T.TEXT)
        x += 22
        cv.put(yy, x, pad(str(r.exchange) if r.exchange else "·", 5), T.DIM)
        x += 6
        cv.put(yy, x, pad(ago(r.idle_s) if r.idle_s is not None else "—", 9), T.DIM)
        x += 10
        cv.put(yy, x, clip(r.verdict or "", max(0, W - path_w - 4 - x)), T.DIM)
        cv.put(yy, W - path_w - 2, clip(tilde(r.repo), path_w), T.FAINT)
    return room


def splash(cv: Canvas, snap: Snapshot, t: float, start: float) -> None:
    W, H = cv.w, cv.h
    cv.fill(0, 0, H, W, Style(T.DEFAULT, T.CANVAS_BG))
    lw = logo_width()
    y = max(1, H // 2 - 4)
    if W >= lw + 2:
        big_logo(cv, y, (W - lw) // 2, t)
    else:
        gradient_text(cv, y, max(0, (W - 12) // 2), "AGENT·BRIDGE", t)
    elapsed = t - start
    lines: list[list[Part]] = []
    for i, role in enumerate(("planner", "supervisor", "builder")):
        if elapsed < 0.15 + i * 0.12:
            break
        rv = snap.roles.get(role)
        what = f"{ENGINE_LABEL.get(rv.engine, rv.engine)} · {short_model(rv.model)}" if rv else "not configured"
        lines.append([(f"{T.GLYPH[role]} {role:<12}", Style(T.ROLE[role], bold=True)), (f"{what:<34}", T.DIM), ("linked", T.OK)])
    left = (W - 54) // 2
    for i, row in enumerate(lines):
        parts(cv, y + 4 + i, max(0, left), row)
    bar_w = min(40, W - 4)
    frac = min(1.0, elapsed / 0.8)
    bx = (W - bar_w) // 2
    cv.put(y + 8, bx, "━" * int(bar_w * frac), Style(T.GRADIENT[int(t * 10) % len(T.GRADIENT)]))
    cv.put(y + 8, bx + int(bar_w * frac), "─" * (bar_w - int(bar_w * frac)), T.GHOST)


def too_small(cv: Canvas) -> None:
    msg = "agent-bridge needs a terminal of at least 60×16"
    cv.fill(0, 0, cv.h, cv.w, Style(T.DEFAULT, T.CANVAS_BG))
    cv.put(cv.h // 2, max(0, (cv.w - width(msg)) // 2), clip(msg, cv.w), T.WARN)


def now_t() -> float:
    return time.monotonic()
