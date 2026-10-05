from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agent_bridge.config import (
    ConfigError,
    config_from_dict,
    format_duration,
    load_config,
    parse_duration,
    render_config_template,
)


def write(repo: Path, text: str) -> Path:
    path = repo / "bridge.toml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(90, 90), ("90", 90), ("90s", 90), ("30m", 1800), ("3h", 10800), ("1d", 86400), ("1h30m", 5400), ("2h 5m", 7500)],
)
def test_parse_duration(value: object, seconds: int) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "3x", "h", "-5", True, 1.5, "1.5h", -1])
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_format_duration() -> None:
    assert format_duration(86400) == "1d"
    assert format_duration(10800) == "3h"
    assert format_duration(90) == "90s"


def test_minimal_config_gets_the_documented_defaults(repo: Path) -> None:
    cfg = load_config(write(repo, "version = 1\n"))
    assert cfg.project.name == "repo"
    assert cfg.project.repo == repo.resolve()
    assert cfg.project.prd == repo.resolve() / "docs/PRD.md"
    assert cfg.project.decisions.name == "DECISIONS.md"
    assert cfg.project.mode == "autonomous"
    for role in ("planner", "supervisor", "builder"):
        assert cfg.role(role).engine == "claude-code"
    assert cfg.role("planner").model == "claude-fable-5-1"
    assert cfg.role("planner").variant == "max"
    assert cfg.role("supervisor").variant == "xhigh"
    assert cfg.role("builder").model == "claude-opus-5-5"
    assert cfg.role("builder").timeout == 3 * 3600
    assert cfg.role("supervisor").read_only and cfg.role("planner").read_only
    assert not cfg.role("builder").read_only
    assert cfg.git_push == "never" and cfg.billing_mode == "subscription"
    assert cfg.wait_max == 86400
    assert cfg.budget.max_unchanged_exchanges == 12 and cfg.budget.max_replans_per_phase == 3
    assert cfg.rotation.builder_on_phase_complete is True
    assert cfg.rotation.phase_complete_pattern.search("Phase 2 complete")
    assert not cfg.uses_opencode()
    assert cfg.owner_only_settings() == []


def test_unknown_keys_and_tables_are_errors(repo: Path) -> None:
    text = "version = 1\nmistake = 2\n[project]\nprdd = 'x'\n[builder]\nengnie = 'opencode'\n[extra]\n"
    with pytest.raises(ConfigError) as err:
        load_config(write(repo, text))
    joined = " | ".join(err.value.problems)
    assert "(top level).mistake: unknown key" in joined
    assert "project.prdd: unknown key" in joined
    assert "builder.engnie: unknown key" in joined
    assert "(top level).extra: unknown key" in joined


def test_type_and_choice_errors_are_reported_together(repo: Path) -> None:
    text = (
        "version = 1\n[project]\nmode = 'yolo'\n[builder]\nengine = 'cursor'\ntimeout = 'soon'\n"
        "[git]\npush = true\n[budget]\nmax_exchanges = 0\n"
    )
    with pytest.raises(ConfigError) as err:
        load_config(write(repo, text))
    problems = err.value.problems
    assert any("project.mode" in p for p in problems)
    assert any("builder.engine" in p for p in problems)
    assert any("builder.timeout" in p for p in problems)
    assert any("git.push" in p and "boolean" in p for p in problems)
    assert any("budget.max_exchanges" in p for p in problems)


def test_missing_or_wrong_version(repo: Path) -> None:
    with pytest.raises(ConfigError, match="version: missing"):
        load_config(write(repo, "[project]\n"))
    with pytest.raises(ConfigError, match="version: 2 is not supported"):
        load_config(write(repo, "version = 2\n"))


def test_invalid_toml_and_missing_file(repo: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(repo, "version = = 1\n"))
    with pytest.raises(ConfigError, match="agent-bridge init"):
        load_config(repo / "nope.toml")


def test_project_paths_must_stay_inside_the_repo(repo: Path) -> None:
    # The planner writes bridge.toml, and the bridge writes the files it names.
    text = "version = 1\n[project]\nprd = '../../etc/passwd'\nrules = ['/tmp/x.md']\n"
    with pytest.raises(ConfigError) as err:
        load_config(write(repo, text))
    joined = " | ".join(err.value.problems)
    assert "project.prd" in joined and "outside the repo" in joined
    assert "project.rules" in joined


def test_opencode_role_requires_an_explicit_port(repo: Path) -> None:
    with pytest.raises(ConfigError, match="opencode.port: required"):
        load_config(write(repo, "version = 1\n[builder]\nengine = 'opencode'\n"))
    cfg = load_config(write(repo, "version = 1\n[builder]\nengine = 'opencode'\n[opencode]\nport = 4110\n"))
    assert cfg.uses_opencode() and cfg.opencode.port == 4110 and cfg.opencode.accept == "1.18"


def test_engine_model_normalization(repo: Path) -> None:
    text = (
        "version = 1\n[builder]\nengine = 'opencode'\nmodel = 'claude-opus-5-5'\n"
        "[supervisor]\nmodel = 'anthropic/claude-fable-5-1'\n[opencode]\nport = 4111\n"
    )
    cfg = load_config(write(repo, text))
    assert cfg.role("builder").engine_model() == "anthropic/claude-opus-5-5"
    assert cfg.role("supervisor").engine_model() == "claude-fable-5-1"


def test_claude_effort_is_checked(repo: Path) -> None:
    with pytest.raises(ConfigError, match="supervisor.variant"):
        load_config(write(repo, "version = 1\n[supervisor]\nvariant = 'ultrathink'\n"))
    cfg = load_config(write(repo, "version = 1\n[builder]\nengine = 'opencode'\nvariant = 'high'\n[opencode]\nport = 4112\n"))
    assert cfg.role("builder").variant == "high"


def test_owner_only_settings_are_listed(repo: Path) -> None:
    cfg = load_config(write(repo, "version = 1\n[git]\npush = 'allowed'\n[billing]\nmode = 'api-key'\n"))
    assert cfg.owner_only_settings() == ['git.push = "allowed"', 'billing.mode = "api-key"']


def test_bad_regexes_are_errors(repo: Path) -> None:
    text = "version = 1\n[rotation]\nphase_complete_pattern = '('\n[safety]\ndanger_commands = ['git push', '[']\n"
    with pytest.raises(ConfigError) as err:
        load_config(write(repo, text))
    joined = " | ".join(err.value.problems)
    assert "rotation.phase_complete_pattern" in joined
    assert "safety.danger_commands[1]" in joined


def test_contract_and_planning_doc_lists(repo: Path) -> None:
    cfg = load_config(write(repo, "version = 1\n[project]\nrules = ['CLAUDE.md', 'docs/STYLE.md']\n"))
    root = repo.resolve()
    assert cfg.contract_files() == [root / "docs/PRD.md", root / "CLAUDE.md", root / "docs/STYLE.md", root / "bridge.toml"]
    assert cfg.planning_docs() == [
        root / "docs/PRD.md",
        root / "CLAUDE.md",
        root / "docs/DECISIONS.md",
        root / "docs/OPEN.md",
        root / "bridge.toml",
    ]


def test_template_round_trips_through_the_validator(repo: Path) -> None:
    text = render_config_template(name='Quote "q" project', created="2026-10-06 12:00", worklog="docs/WORKLOG.md")
    tomllib.loads(text)
    cfg = load_config(write(repo, text))
    assert cfg.project.name == 'Quote "q" project'
    assert cfg.project.worklog == repo.resolve() / "docs/WORKLOG.md"
    assert cfg.role("builder").engine == "claude-code"
    assert not cfg.uses_opencode()
    assert "# [opencode]" in text


def test_template_with_an_opencode_role(repo: Path) -> None:
    text = render_config_template(
        name="x",
        created="now",
        roles={"builder": ("opencode", "anthropic/claude-opus-5-5")},
        opencode_port=4120,
    )
    cfg = load_config(write(repo, text))
    assert cfg.role("builder").engine == "opencode" and cfg.opencode.port == 4120
    assert cfg.role("builder").variant is None
    with pytest.raises(ValueError, match="port is required"):
        render_config_template(name="x", created="now", roles={"builder": ("opencode", "m")})


def test_config_from_dict_matches_load(repo: Path) -> None:
    cfg = config_from_dict({"version": 1, "project": {"name": "n"}}, repo / "bridge.toml")
    assert cfg.project.name == "n"


def test_template_drops_the_default_effort_for_a_custom_model(repo: Path) -> None:
    text = render_config_template(name="x", created="now", roles={"supervisor": ("claude-code", "claude-haiku-4-5-20251001")})
    cfg = load_config(write(repo, text))
    assert cfg.role("supervisor").variant is None
    assert cfg.role("planner").variant == "max"
