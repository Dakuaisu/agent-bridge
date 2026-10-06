"""Changing engines in an existing project: the bridge.toml rewrite, `agent-bridge engines`, and the UI form."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tui_scenes as S

from agent_bridge import contract
from agent_bridge.config import config_from_dict, load_config, render_config_template, rewrite_roles
from agent_bridge.engine import State
from agent_bridge.statedir import BridgeLock
from agent_bridge.tui.modals import Form

from test_cli import Fakes, contract_files, fakes, run  # noqa: F401  (fixture re-export)
from test_tui import app_for, keys, last_argv, type_text


def load(text: str, where: Path) -> object:
    return config_from_dict(tomllib.loads(text), where / "bridge.toml")


def test_the_rewrite_changes_only_the_role_lines(tmp_path: Path) -> None:
    text = render_config_template(name="x", created="now")
    new = rewrite_roles(text, {"builder": ("opencode", "anthropic/claude-opus-5-5")}, opencode_port=4120)
    cfg = load(new, tmp_path)
    assert (cfg.role("builder").engine, cfg.role("builder").model, cfg.opencode.port) == ("opencode", "anthropic/claude-opus-5-5", 4120)
    assert cfg.role("planner").variant == "max" and cfg.role("supervisor").variant == "xhigh"
    changed = [line for line in new.splitlines() if line not in text.splitlines()]
    assert changed == [
        'engine = "opencode"',
        'model = "anthropic/claude-opus-5-5"                   # on opencode, a bare id gets "anthropic/"',
        "[opencode]                             # only when a role uses opencode",
        "port = 4120                            # one server per project",
    ]


def test_the_default_effort_follows_the_default_model(tmp_path: Path) -> None:
    text = render_config_template(name="x", created="now")
    haiku = rewrite_roles(text, {"planner": ("claude-code", "claude-haiku-4-5-20251001")})
    assert load(haiku, tmp_path).role("planner").variant is None and '# variant = "max"' in haiku
    back = rewrite_roles(haiku, {"planner": ("claude-code", "claude-fable-5-1")})
    assert load(back, tmp_path).role("planner").variant == "max"


def test_a_file_without_role_tables_gets_them(tmp_path: Path) -> None:
    new = rewrite_roles("version = 1\n[project]\nname = 'demo'\n", {"builder": ("opencode", "anthropic/claude-opus-5-5")}, opencode_port=4130)
    cfg = load(new, tmp_path)
    assert cfg.role("builder").engine == "opencode" and cfg.opencode.port == 4130


def adopted(repo: Path) -> None:
    contract_files(repo)
    (repo / "bridge.toml").write_text(render_config_template(name="demo", created="now"))
    assert run("approve", "--no-run", "--repo", str(repo)) == 0


def test_engines_shows_and_changes_and_reapproves(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    adopted(repo)
    st = State.load(repo / ".bridge/state.json")
    st.sessions["builder"] = {"engine": "claude-code", "id": "b-1", "closed": False}
    st.sessions["supervisor"] = {"engine": "claude-code", "id": "s-1", "closed": False}
    st.save(repo / ".bridge/state.json")
    capsys.readouterr()
    assert run("engines", "--repo", str(repo)) == 0
    assert "builder:     claude-code claude-opus-5-5" in capsys.readouterr().out
    assert run("engines", "--builder", "opencode:anthropic/claude-opus-5-5", "--repo", str(repo)) == 0
    out = capsys.readouterr().out
    assert "builder: claude-code claude-opus-5-5 -> opencode anthropic/claude-opus-5-5" in out and "recorded DEC-" in out
    cfg = load_config(repo / "bridge.toml")
    assert cfg.role("builder").engine == "opencode" and 4100 <= (cfg.opencode.port or 0) < 4200
    ledger = (repo / "docs/DECISIONS.md").read_text()
    assert "Engines changed by the owner" in ledger and "OWNER DECISION (agent-bridge engines)" in ledger
    st = State.load(repo / ".bridge/state.json")
    assert st.sessions["builder"]["closed"] is True and st.sessions["supervisor"]["closed"] is False
    assert contract.drifted(cfg, st.contract["hashes"]) == []
    assert run("engines", "--builder", "opencode:claude-opus-5-5", "--repo", str(repo)) == 0
    assert "nothing to change" in capsys.readouterr().out


def test_engines_does_not_approve_other_changes(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    adopted(repo)
    (repo / "docs/PRD.md").write_text((repo / "docs/PRD.md").read_text() + "\n<!-- owner edit -->\n")
    capsys.readouterr()
    assert run("engines", "--planner", "claude-code:claude-haiku-4-5-20251001", "--repo", str(repo)) == 0
    assert "the contract also changed elsewhere" in capsys.readouterr().out
    assert "Engines changed by the owner" not in (repo / "docs/DECISIONS.md").read_text()
    st = State.load(repo / ".bridge/state.json")
    assert sorted(contract.drifted(load_config(repo / "bridge.toml"), st.contract["hashes"])) == ["bridge.toml", "docs/PRD.md"]


def test_engines_refuses_while_running_and_rejects_bad_specs(repo: Path, fakes: Fakes, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    adopted(repo)
    lock = BridgeLock(repo / ".bridge/lock")
    lock.acquire({"argv": ["run"]})
    try:
        assert run("engines", "--builder", "opencode:anthropic/claude-opus-5-5", "--repo", str(repo)) == 2
        assert "stop it first" in capsys.readouterr().err
    finally:
        lock.release()
    assert run("engines", "--builder", "cursor:gpt-x", "--repo", str(repo)) == 2
    assert load_config(repo / "bridge.toml").role("builder").engine == "claude-code"


def test_the_ui_form_starts_from_the_current_engines(tmp_path: Path) -> None:
    repo, _ = S.running(tmp_path, hold_lock=False)
    app = app_for(repo)
    app.key("e")
    form = app.modals[-1]
    assert isinstance(form, Form) and form.title == "CHANGE ENGINES"
    assert [f.value for f in form.fields[1:4]] == ["claude-code:claude-fable-5-1", "claude-code:claude-fable-5-1", "opencode:anthropic/claude-opus-5-5"]
    app.key("ctrl-s")
    assert form.error == "nothing changed"
    keys(app, "tab")
    for _ in range(40):
        app.key("backspace")
    type_text(app, "claude-code:claude-haiku-4-5-20251001")
    app.key("ctrl-s")
    assert last_argv(app) == [
        "engines",
        "--planner", "claude-code:claude-haiku-4-5-20251001",
        "--supervisor", "claude-code:claude-fable-5-1",
        "--builder", "opencode:anthropic/claude-opus-5-5",
        "--port", "4120",
        "--repo", str(app.repo),
    ]


def test_the_ui_form_presets_and_its_refusal_while_running(tmp_path: Path) -> None:
    repo, lock = S.running(tmp_path)
    try:
        app = app_for(repo)
        app.key("e")
        assert not app.modals and "stop the bridge first" in app.toast_items[-1][0]
    finally:
        assert lock
        lock.release()
    app = app_for(repo)
    app.key("e")
    keys(app, "right", "ctrl-s")
    assert last_argv(app) == [
        "engines",
        "--planner", "claude-code:claude-fable-5-1",
        "--supervisor", "claude-code:claude-fable-5-1",
        "--builder", "claude-code:claude-opus-5-5",
        "--repo", str(app.repo),
    ]
