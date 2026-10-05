"""Fake `claude` and `opencode` executables: scripted output, and a record of every call."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

SCRIPT = r'''#!{python}
import json, os, sys, time
scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
record = scenario["record"]
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read() if ("-p" in args or "run" in args[:1]) else ""
calls = [json.loads(l) for l in open(record)] if os.path.exists(record) else []
entry = {{"args": args, "cwd": os.getcwd(), "stdin": stdin,
          "env": {{k: (k in os.environ) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "KAGGLE_API_TOKEN")}}}}
with open(record, "a") as f:
    f.write(json.dumps(entry) + "\n")
if args[:1] == ["--version"]:
    print(scenario.get("version", "0.0.0")); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps(scenario.get("auth", {{"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}}))); sys.exit(0)
turns = [c for c in calls if c["args"][:1] in (["-p"], ["run"])]
turn = scenario["turns"][min(len(turns), len(scenario["turns"]) - 1)]
for item in turn.get("lines", []):
    if isinstance(item, dict) and "sleep" in item and len(item) == 1:
        time.sleep(item["sleep"]); continue
    print(json.dumps(item) if not isinstance(item, str) else item, flush=True)
time.sleep(turn.get("sleep", 0))
sys.stderr.write(turn.get("stderr", ""))
sys.exit(turn.get("exit", 0))
'''


def install(bindir: Path, name: str) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text(SCRIPT.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def scenario(tmp: Path, turns: list[dict[str, Any]], **extra: Any) -> Path:
    path = tmp / "scenario.json"
    path.write_text(json.dumps({"record": str(tmp / "calls.jsonl"), "turns": turns, **extra}))
    os.environ["FAKE_SCENARIO"] = str(path)
    return tmp / "calls.jsonl"


def calls(record: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def claude_turn(text: str, *, model: str = "claude-fable-5-1", session: str = "s", tools: list[dict[str, Any]] | None = None, usage: dict[str, int] | None = None, api_key_source: str = "none") -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "tool_use", "name": t["name"], "input": t.get("input", {})} for t in tools or []]
    lines: list[Any] = [{"type": "system", "subtype": "init", "session_id": session, "model": model, "apiKeySource": api_key_source}]
    if content:
        lines.append({"type": "assistant", "message": {"model": model, "content": content, "usage": usage or {}}})
    lines.append({"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": text}], "usage": usage or {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}})
    lines.append({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session})
    return {"lines": lines}


def claude_error(message: str) -> dict[str, Any]:
    return {"lines": [{"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none"}, {"type": "result", "subtype": "success", "is_error": True, "result": message}]}
