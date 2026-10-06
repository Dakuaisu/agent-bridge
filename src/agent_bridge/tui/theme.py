"""Neon-on-dark palette for the terminal UI, in xterm-256 colours with a basic-colour fallback."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

INHERIT = -2  # background: keep whatever is already painted in that cell
DEFAULT = -1  # the terminal's own colour


@dataclass(frozen=True)
class Style:
    fg: int = DEFAULT
    bg: int = INHERIT
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False

    def on(self, bg: int) -> Style:
        return replace(self, bg=bg)

    def fg_(self, fg: int) -> Style:
        return replace(self, fg=fg)

    def b(self) -> Style:
        return replace(self, bold=True)


# Roles keep one colour everywhere: pods, stream badges, borders.
ROLE = {"planner": 141, "supervisor": 45, "builder": 213, "owner": 214, "bridge": 110}
ROLE_DIM = {"planner": 60, "supervisor": 31, "builder": 96, "owner": 136, "bridge": 66}
GLYPH = {"planner": "◇", "supervisor": "◆", "builder": "◈", "owner": "◎", "bridge": "◌"}

RAMP = {
    "cyan": (23, 30, 37, 44, 51),
    "green": (22, 28, 34, 41, 48),
    "violet": (54, 61, 98, 141, 183),
    "pink": (89, 125, 162, 206, 213),
    "amber": (94, 130, 166, 208, 214),
    "red": (52, 88, 124, 160, 203),
}
ROLE_RAMP = {"planner": "violet", "supervisor": "cyan", "builder": "pink", "owner": "amber", "bridge": "cyan"}

# Cyan through violet to magenta and back, so a moving gradient never jumps.
GRADIENT = (51, 45, 39, 33, 63, 99, 135, 171, 207, 171, 135, 99, 63, 33, 39, 45)

CANVAS_BG = 233
PANEL_BG = 234
MODAL_BG = 235
FIELD_BG = 237
SELECT_BG = 237

TEXT = Style(252)
BRIGHT = Style(255)
DIM = Style(245)
FAINT = Style(240)
GHOST = Style(237)
BORDER = Style(239)
ACCENT = Style(51, bold=True)
ACCENT_SOFT = Style(87)
OK = Style(84)
WARN = Style(214)
ERR = Style(203)
INFO = Style(39)
KEY = Style(16, 87, bold=True)
KEY_OFF = Style(244, 238)
KEY_LABEL = Style(250)
KEY_LABEL_OFF = Style(241)


def chip(fg_bg: tuple[int, int]) -> Style:
    return Style(fg_bg[0], fg_bg[1], bold=True)


STATE_CHIP = {
    "running": (16, 48),
    "working": (16, 48),
    "planning": (16, 141),
    "waiting": (16, 39),
    "sleeping": (16, 75),
    "paused": (16, 203),
    "complete": (16, 84),
    "review": (16, 213),
    "answers": (16, 214),
    "idle": (16, 245),
    "new": (16, 87),
    "stopped": (16, 245),
}


def ramp(name: str, level: float) -> int:
    colours = RAMP[name]
    return colours[min(len(colours) - 1, max(0, int(level * (len(colours) - 1) + 0.5)))]


def pulse(t: float, period: float = 1.6) -> float:
    """0..1..0 over one period."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (t % period) / period)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner(t: float) -> str:
    return SPINNER[int(t * 12) % len(SPINNER)]


# ----------------------------------------------------------------- xterm colours

_BASE16 = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


def xterm_rgb(n: int) -> tuple[int, int, int]:
    if n < 16:
        return _BASE16[n]
    if n < 232:
        n -= 16
        level = (0, 95, 135, 175, 215, 255)
        return level[n // 36], level[(n // 6) % 6], level[n % 6]
    v = 8 + (n - 232) * 10
    return v, v, v


def nearest_basic(n: int, colours: int = 8) -> int:
    """The closest of the first `colours` palette entries, for terminals without 256 colours."""
    if n < 0:
        return n
    if n < colours:
        return n
    r, g, b = xterm_rgb(n)
    best, best_d = 7, 1 << 30
    for i in range(colours):
        cr, cg, cb = _BASE16[i]
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_d:
            best, best_d = i, d
    return best
