"""The live view: one renderer for the foreground run and `logs` (docs/DESIGN.md section 10)."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Callable

ROLE_PAD = 10

# The engine prints these itself on the console, so the foreground view skips them.
CONSOLE_KINDS = {"verdict", "wait_start", "wait_end", "sleep_start", "paused", "complete", "turn_error"}


def _clock(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"


def _clip(text: str, width: int = 160) -> str:
    first = " ".join(text.strip().split())
    return first if len(first) <= width else first[: width - 3] + "..."


def render_event(e: dict[str, Any], *, foreground: bool = False) -> str | None:
    kind = e.get("kind")
    if foreground and kind in CONSOLE_KINDS:
        return None
    t = f"[{_clock(e.get('ts', ''))}]"
    role = str(e.get("role", ""))
    if kind == "turn_start":
        session = e.get("session") or "new session"
        return f"{t} == exchange {e.get('exchange')} == {role} ({e.get('engine')}, {session})"
    if kind == "agent":
        sub = e.get("type")
        if sub == "tool":
            return f"{t}   {role:<{ROLE_PAD}} > {e.get('tool')}: {_clip(e.get('text', ''), 140)}"
        if sub == "text":
            return f"{t}   {role:<{ROLE_PAD}} | {_clip(e.get('text', ''))}"
        if sub in ("status", "error"):
            return f"{t}   {role:<{ROLE_PAD}} ! {_clip(e.get('text', ''))}"
        return None
    if kind == "turn_end":
        ctx = f", ctx {e['context_tokens']:,} tokens" if e.get("context_tokens") else ""
        models = f", served by {', '.join(e.get('models') or [])}" if e.get("models") else ""
        return f"{t}   {role:<{ROLE_PAD}} + done in {format_seconds(e.get('seconds', 0))}{ctx}{models}"
    if kind == "builder_report":
        changed = "repo changed" if e.get("changed") else "repo unchanged"
        wait = f"; asks for {e['wait']}" if e.get("wait") else ""
        return f"{t}   builder    + report: {e.get('chars')} chars, {changed}, {e.get('decisions', 0)} decisions needed{wait}"
    if kind == "review":
        return f"{t}   !! {e.get('title')}"
    if kind == "verdict":
        return f"{t}   supervisor VERDICT: {_clip(e.get('verdict', ''))}"
    if kind == "wait_start":
        return f"{t}   bridge     {e.get('spec')} (asked by the {e.get('source')}; ends by {e.get('deadline')}): sleeping, no model calls"
    if kind == "wait_end":
        return f"{t}   bridge     wait over: {e.get('outcome')}"
    if kind == "sleep_start":
        return f"{t}   bridge     sleeping until {e.get('until')} ({e.get('reason')})"
    if kind == "paused":
        return f"{t}   bridge     PAUSED ({e.get('reason')}): {_clip(e.get('detail', ''))}"
    if kind == "complete":
        return f"{t}   bridge     PROJECT COMPLETE at exchange {e.get('exchange')}"
    if kind in ("kickoff", "owner_message"):
        return f"{t}   owner      {_clip(e.get('text', ''))}"
    if kind == "replan":
        return f"{t}   supervisor REPLAN ({e.get('phase')}): {_clip(e.get('problem', ''))}"
    if kind == "plan_change":
        return f"{t}   planner    {e.get('id')} {e.get('outcome')} ({e.get('dec')})"
    if kind == "session_retired":
        return f"{t}   bridge     retired the {role} session {e.get('id')} ({e.get('reason')})"
    if kind == "launch":
        return f"{t} === launch: agent-bridge {' '.join(e.get('argv') or [])}"
    return None


def format_seconds(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def foreground_printer(write: Callable[[str], None] | None = None) -> Callable[[dict[str, Any]], None]:
    out = write or (lambda line: print(line, file=sys.stdout, flush=True))

    def listener(event: dict[str, Any]) -> None:
        line = render_event(event, foreground=True)
        if line:
            out(line)

    return listener
