"""bridge.toml: load and validate the project configuration (docs/DESIGN.md section 9)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

CONFIG_NAME = "bridge.toml"
SCHEMA_VERSION = 1
ROLES = ("planner", "supervisor", "builder")
ENGINES = ("claude-code", "opencode")
MODES = ("autonomous", "escalate")
BILLING_MODES = ("subscription", "api-key")
PUSH_POLICIES = ("never", "allowed")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "planner": {"engine": "claude-code", "model": "claude-fable-5-1", "variant": "max", "timeout": "1h"},
    "supervisor": {"engine": "claude-code", "model": "claude-fable-5-1", "variant": "xhigh", "timeout": "30m"},
    "builder": {"engine": "claude-code", "model": "claude-opus-5-5", "variant": None, "timeout": "3h"},
}
DEFAULT_PHASE_PATTERN = r"(?i)\bphase\s+[\w.-]+\s+(?:is\s+)?complete\b"


class ConfigError(Exception):
    def __init__(self, problems: list[str], path: Path | None = None) -> None:
        self.problems = problems
        self.path = path
        where = f"{path}: " if path else ""
        super().__init__(where + "; ".join(problems))


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_FULL = re.compile(r"^(?:\d+\s*[smhd]\s*)+$")


def parse_duration(value: object) -> int:
    """Seconds from 90, "90", "90s", "30m", "3h", "1d" or "1h30m"."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"negative duration: {value}")
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            return int(text)
        if _DURATION_FULL.match(text):
            return sum(int(n) * _DURATION_UNITS[u] for n, u in re.findall(r"(\d+)\s*([smhd])", text))
    raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 30m, 3h, 1d, 1h30m)")


def format_duration(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0 and seconds:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@dataclass(frozen=True)
class RoleConfig:
    role: str
    engine: str
    model: str
    variant: str | None
    timeout: int

    @property
    def read_only(self) -> bool:
        # A role property, not a setting: a writable planner or supervisor breaks the audit model.
        return self.role != "builder"

    def engine_model(self) -> str:
        if self.engine == "opencode":
            return self.model if "/" in self.model else f"anthropic/{self.model}"
        return self.model.removeprefix("anthropic/")


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    repo: Path
    prd: Path
    rules: tuple[Path, ...]
    decisions: Path
    open_items: Path
    results: Path
    mode: str
    worklog: Path | None
    phases: str | None
    supervisor_rules: Path | None
    env_file: Path | None
    verify: str | None = None
    verify_timeout: int = 900


@dataclass(frozen=True)
class OpencodeConfig:
    port: int | None = None
    accept: str = "1.18"


@dataclass(frozen=True)
class BudgetConfig:
    max_exchanges: int | None = None
    max_wall_time: int | None = None
    max_unchanged_exchanges: int | None = 12
    max_replans_per_phase: int = 3
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class RotationConfig:
    builder_max_context_tokens: int = 600_000
    builder_on_phase_complete: bool = True
    supervisor_max_context_tokens: int = 400_000
    supervisor_on_phase_complete: bool = True
    planner_max_context_tokens: int = 400_000
    phase_complete_pattern: re.Pattern[str] = field(default_factory=lambda: re.compile(DEFAULT_PHASE_PATTERN))


@dataclass(frozen=True)
class SafetyConfig:
    danger_commands: tuple[re.Pattern[str], ...] = ()
    caffeinate: bool = True
    sandbox: str = "auto"
    sandbox_writable: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyConfig:
    desktop: bool = True
    command: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    version: int
    project: ProjectConfig
    roles: dict[str, RoleConfig]
    opencode: OpencodeConfig
    billing_mode: str
    git_push: str
    wait_max: int
    budget: BudgetConfig
    rotation: RotationConfig
    safety: SafetyConfig
    notify: NotifyConfig = NotifyConfig()

    def role(self, name: str) -> RoleConfig:
        return self.roles[name]

    def uses_opencode(self) -> bool:
        return any(r.engine == "opencode" for r in self.roles.values())

    def contract_files(self) -> list[Path]:
        """The files whose hashes are approved: the PRD, the rules and this config."""
        return [self.project.prd, *self.project.rules, self.path]

    def planning_docs(self) -> list[Path]:
        """The only files the planner may have the bridge write."""
        p = self.project
        return [p.prd, p.rules[0], p.decisions, p.open_items, self.path]

    def owner_only_settings(self) -> list[str]:
        found = []
        if self.git_push == "allowed":
            found.append('git.push = "allowed"')
        if self.billing_mode == "api-key":
            found.append('billing.mode = "api-key"')
        if self.project.verify:
            found.append(f"project.verify = {self.project.verify!r}")
        if self.safety.sandbox == "off":
            found.append('safety.sandbox = "off"')
        return found

    def owner_settings(self) -> tuple[Any, ...]:
        """What only the owner may change: a re-plan that changes any of it is refused."""
        return (self.git_push, self.billing_mode, self.project.verify, self.safety.sandbox, self.safety.sandbox_writable, self.notify.command)


class _Table:
    """One TOML table being validated; remembers which keys were read so leftovers are errors."""

    def __init__(self, data: Any, where: str, problems: list[str]) -> None:
        if not isinstance(data, dict):
            problems.append(f"[{where}] must be a table")
            data = {}
        self.data: dict[str, Any] = data
        self.where = where
        self.problems = problems
        self.used: set[str] = set()

    def _bad(self, key: str, msg: str) -> None:
        self.problems.append(f"{self.where}.{key}: {msg}")

    def has(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, kind: type | tuple[type, ...], default: Any, *, choices: tuple[str, ...] | None = None) -> Any:
        self.used.add(key)
        if key not in self.data:
            return default
        value = self.data[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if isinstance(value, bool) and bool not in kinds:
            self._bad(key, f"expected {_kind_name(kinds)}, got a boolean")
            return default
        if not isinstance(value, kinds):
            self._bad(key, f"expected {_kind_name(kinds)}, got {type(value).__name__}")
            return default
        if choices is not None and value not in choices:
            self._bad(key, f"must be one of {', '.join(choices)}; got {value!r}")
            return default
        return value

    def duration(self, key: str, default: int | None) -> int | None:
        self.used.add(key)
        if key not in self.data:
            return default
        try:
            return parse_duration(self.data[key])
        except ValueError as e:
            self._bad(key, str(e))
            return default

    def positive_int(self, key: str, default: int | None) -> int | None:
        value = self.get(key, int, default)
        if value is not None and value <= 0:
            self._bad(key, f"must be positive; got {value}")
            return default
        return value

    def regex(self, key: str, default: str) -> re.Pattern[str]:
        text = self.get(key, str, default)
        try:
            return re.compile(text)
        except re.error as e:
            self._bad(key, f"invalid regular expression: {e}")
            return re.compile(default)

    def finish(self) -> None:
        for key in self.data:
            if key not in self.used:
                self._bad(key, "unknown key")


def _kind_name(kinds: tuple[type, ...]) -> str:
    names = {str: "a string", int: "an integer", bool: "a boolean", list: "an array", dict: "a table"}
    return " or ".join(names.get(k, k.__name__) for k in kinds)


def _inside(repo: Path, rel: str, key: str, problems: list[str]) -> Path:
    path = (repo / rel).resolve()
    if not path.is_relative_to(repo):
        problems.append(f"project.{key}: {rel!r} resolves outside the repo ({repo})")
    return path


def load_config(path: Path) -> Config:
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError([f"no {CONFIG_NAME} here; run `agent-bridge init` or pass --config"], path) from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError([f"not valid TOML: {e}"], path) from None
    return config_from_dict(data, path)


def config_from_dict(data: dict[str, Any], path: Path) -> Config:
    problems: list[str] = []
    path = Path(path).resolve()
    top = _Table(data, "(top level)", problems)

    version = top.get("version", int, None)
    if version is None:
        problems.append("version: missing; this file format is version 1")
    elif version != SCHEMA_VERSION:
        problems.append(f"version: {version} is not supported (this agent-bridge reads version {SCHEMA_VERSION})")

    pt = _Table(top.get("project", dict, {}), "project", problems)
    name = pt.get("name", str, "") or path.parent.name
    repo = (path.parent / pt.get("repo", str, ".")).resolve()
    rules_raw = pt.get("rules", list, ["CLAUDE.md"])
    if not rules_raw or not all(isinstance(r, str) for r in rules_raw):
        problems.append("project.rules: must be a non-empty array of paths")
        rules_raw = ["CLAUDE.md"]

    def opt_path(key: str) -> Path | None:
        rel = pt.get(key, str, None)
        return None if rel is None else _inside(repo, rel, key, problems)

    project = ProjectConfig(
        name=name,
        repo=repo,
        prd=_inside(repo, pt.get("prd", str, "docs/PRD.md"), "prd", problems),
        rules=tuple(_inside(repo, r, "rules", problems) for r in rules_raw),
        decisions=_inside(repo, pt.get("decisions", str, "docs/DECISIONS.md"), "decisions", problems),
        open_items=_inside(repo, pt.get("open_items", str, "docs/OPEN.md"), "open_items", problems),
        results=_inside(repo, pt.get("results", str, "docs/RESULTS.md"), "results", problems),
        mode=pt.get("mode", str, "autonomous", choices=MODES),
        worklog=opt_path("worklog"),
        phases=pt.get("phases", str, None),
        supervisor_rules=opt_path("supervisor_rules"),
        env_file=opt_path("env_file"),
        verify=pt.get("verify", str, None) or None,
        verify_timeout=pt.duration("verify_timeout", 900) or 900,
    )
    pt.finish()

    roles: dict[str, RoleConfig] = {}
    for role in ROLES:
        d = ROLE_DEFAULTS[role]
        rt = _Table(top.get(role, dict, {}), role, problems)
        engine = rt.get("engine", str, d["engine"], choices=ENGINES)
        model = rt.get("model", str, d["model"]).strip()
        if not model:
            problems.append(f"{role}.model: must not be empty")
            model = d["model"]
        default_variant = d["variant"] if (engine, model) == (d["engine"], d["model"]) else None
        variant = rt.get("variant", str, default_variant)
        if engine == "claude-code" and variant is not None and variant not in CLAUDE_EFFORTS:
            problems.append(f"{role}.variant: Claude Code effort must be one of {', '.join(CLAUDE_EFFORTS)}; got {variant!r}")
            variant = None
        timeout = rt.duration("timeout", parse_duration(d["timeout"]))
        rt.finish()
        roles[role] = RoleConfig(role=role, engine=engine, model=model, variant=variant, timeout=timeout or 1)

    ot = _Table(top.get("opencode", dict, {}), "opencode", problems)
    opencode = OpencodeConfig(port=ot.positive_int("port", None), accept=ot.get("accept", str, "1.18"))
    ot.finish()
    if any(r.engine == "opencode" for r in roles.values()) and opencode.port is None:
        # A default port could land on another project's server (FillingQA uses 4096).
        problems.append("opencode.port: required when a role uses opencode (one port per project; `init` picks a free one)")

    bt = _Table(top.get("billing", dict, {}), "billing", problems)
    billing_mode = bt.get("mode", str, "subscription", choices=BILLING_MODES)
    bt.finish()

    gt = _Table(top.get("git", dict, {}), "git", problems)
    git_push = gt.get("push", str, "never", choices=PUSH_POLICIES)
    gt.finish()

    wt = _Table(top.get("waits", dict, {}), "waits", problems)
    wait_max = wt.duration("max", 86400) or 86400
    wt.finish()

    bu = _Table(top.get("budget", dict, {}), "budget", problems)
    budget = BudgetConfig(
        max_exchanges=bu.positive_int("max_exchanges", None),
        max_wall_time=bu.duration("max_wall_time", None),
        max_unchanged_exchanges=bu.positive_int("max_unchanged_exchanges", 12),
        max_replans_per_phase=bu.positive_int("max_replans_per_phase", 3) or 3,
        max_cost_usd=_positive_number(bu, "max_cost_usd"),
    )
    bu.finish()

    ro = _Table(top.get("rotation", dict, {}), "rotation", problems)
    rotation = RotationConfig(
        builder_max_context_tokens=ro.positive_int("builder_max_context_tokens", 600_000) or 600_000,
        builder_on_phase_complete=ro.get("builder_on_phase_complete", bool, True),
        supervisor_max_context_tokens=ro.positive_int("supervisor_max_context_tokens", 400_000) or 400_000,
        supervisor_on_phase_complete=ro.get("supervisor_on_phase_complete", bool, True),
        planner_max_context_tokens=ro.positive_int("planner_max_context_tokens", 400_000) or 400_000,
        phase_complete_pattern=ro.regex("phase_complete_pattern", DEFAULT_PHASE_PATTERN),
    )
    ro.finish()

    st = _Table(top.get("safety", dict, {}), "safety", problems)
    danger_raw = st.get("danger_commands", list, [])
    danger: list[re.Pattern[str]] = []
    for i, pattern in enumerate(danger_raw):
        if not isinstance(pattern, str):
            problems.append(f"safety.danger_commands[{i}]: must be a string")
            continue
        try:
            danger.append(re.compile(pattern))
        except re.error as e:
            problems.append(f"safety.danger_commands[{i}]: invalid regular expression: {e}")
    writable = st.get("sandbox_writable", list, [])
    if any(not isinstance(w, str) or not w.startswith(("/", "~")) for w in writable):
        problems.append("safety.sandbox_writable: absolute paths (or ~/...) only")
        writable = []
    safety = SafetyConfig(
        danger_commands=tuple(danger),
        caffeinate=st.get("caffeinate", bool, True),
        sandbox=st.get("sandbox", str, "auto", choices=("auto", "on", "off")),
        sandbox_writable=tuple(writable),
    )
    st.finish()

    nt = _Table(top.get("notify", dict, {}), "notify", problems)
    notify = NotifyConfig(desktop=nt.get("desktop", bool, True), command=nt.get("command", str, None) or None)
    nt.finish()

    top.finish()
    if problems:
        raise ConfigError(problems, path)
    return Config(
        path=path,
        version=version or SCHEMA_VERSION,
        project=project,
        roles=roles,
        opencode=opencode,
        billing_mode=billing_mode,
        git_push=git_push,
        wait_max=wait_max,
        budget=budget,
        rotation=rotation,
        safety=safety,
        notify=notify,
    )


def _positive_number(table: _Table, key: str) -> float | None:
    value = table.get(key, (int, float), None)
    if value is not None and value <= 0:
        table._bad(key, f"must be positive; got {value}")
        return None
    return float(value) if value is not None else None


def toml_str(value: str) -> str:
    """A TOML basic string. JSON string escaping is a subset of TOML's."""
    return json.dumps(value)


def render_config_template(
    *,
    name: str,
    created: str,
    prd: str = "docs/PRD.md",
    rules: tuple[str, ...] = ("CLAUDE.md",),
    decisions: str = "docs/DECISIONS.md",
    open_items: str = "docs/OPEN.md",
    results: str = "docs/RESULTS.md",
    worklog: str | None = None,
    supervisor_rules: str | None = None,
    roles: dict[str, tuple[str, str]] | None = None,
    opencode_port: int | None = None,
) -> str:
    """bridge.toml text from templates/bridge.toml; `roles` maps role -> (engine, model)."""
    from importlib.resources import files

    text = files("agent_bridge").joinpath("templates/bridge.toml").read_text(encoding="utf-8")
    values: dict[str, str] = {
        "name": toml_str(name),
        "created": created,
        "prd": toml_str(prd),
        "rules": ", ".join(toml_str(r) for r in rules),
        "decisions": toml_str(decisions),
        "open_items": toml_str(open_items),
        "results": toml_str(results),
        "worklog_line": f"worklog = {toml_str(worklog)}" if worklog else '# worklog = "docs/WORKLOG.md"',
        "supervisor_rules_line": (
            f"supervisor_rules = {toml_str(supervisor_rules)}       # the project's own rules for the supervisor"
            if supervisor_rules
            else '# supervisor_rules = "docs/SUPERVISOR.md"   # the project\'s own rules for the supervisor'
        ),
    }
    chosen = {role: (d["engine"], d["model"]) for role, d in ROLE_DEFAULTS.items()}
    chosen.update(roles or {})
    for role in ROLES:
        engine, model = chosen[role]
        default = ROLE_DEFAULTS[role]
        # Effort levels are model-specific: the default effort only goes with the default model.
        variant = default["variant"] if (engine, model) == (default["engine"], default["model"]) else None
        values[f"{role}_engine"] = toml_str(engine)
        values[f"{role}_model"] = toml_str(model)
        values[f"{role}_variant_line"] = (
            f"variant = {toml_str(variant)}" if variant and engine == "claude-code" else '# variant = "high"'
        )
        values[f"{role}_timeout"] = toml_str(ROLE_DEFAULTS[role]["timeout"])
    uses_opencode = any(engine == "opencode" for engine, _ in chosen.values())
    if uses_opencode and opencode_port is None:
        raise ValueError("a role uses opencode, so an opencode port is required")
    values["opencode_block"] = (
        f"[opencode]\nport = {opencode_port}                              # one server per project\naccept = \"1.18\""
        if uses_opencode
        else "# [opencode]                             # only when a role uses opencode\n"
        "# port = 4099                            # one server per project\n"
        '# accept = "1.18"'
    )
    return Template(text).substitute(values)


# ----------------------------------------------------------------- changing engines in an existing file

_TABLE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
_ANY_TABLE = re.compile(r"^\s*\[")


def _key_line(key: str, commented: bool) -> re.Pattern[str]:
    lead = r"(\s*)#\s*" if commented else r"(\s*)"
    return re.compile(lead + rf"({re.escape(key)})(\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s#]+)(.*)$")


def _section(lines: list[str], table: str) -> tuple[int, int] | None:
    for i, line in enumerate(lines):
        m = _TABLE.match(line)
        if m and m.group(1) == table:
            end = next((j for j in range(i + 1, len(lines)) if _ANY_TABLE.match(lines[j])), len(lines))
            return i, end
    return None


def _set_key(lines: list[str], table: str, key: str, value: str | None) -> None:
    """Set `key = value` in [table], keeping the line's comment; None comments the key out."""
    span = _section(lines, table)
    if span is None:
        if value is None:
            return
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{table}]", f"{key} = {value}"]
        return
    start, end = span
    active, commented = _key_line(key, False), _key_line(key, True)
    for i in range(start + 1, end):
        if m := active.match(lines[i]):
            lines[i] = f"# {lines[i].lstrip()}" if value is None else f"{m.group(1)}{key}{m.group(3)}{value}{m.group(5)}"
            return
    if value is None:
        return
    for i in range(start + 1, end):
        if m := commented.match(lines[i]):
            lines[i] = f"{m.group(1)}{key}{m.group(3)}{value}{m.group(5)}"
            return
    lines.insert(start + 1, f"{key} = {value}")


def _activate_table(lines: list[str], table: str) -> None:
    """Turn a commented-out `# [table]` header (as `init` writes it) into a real one."""
    if _section(lines, table) is not None:
        return
    pattern = re.compile(rf"^(\s*)#\s*(\[\s*{re.escape(table)}\s*\])(.*)$")
    for i, line in enumerate(lines):
        if m := pattern.match(line):
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}"
            return


def rewrite_roles(text: str, roles: dict[str, tuple[str, str]], *, opencode_port: int | None = None) -> str:
    """bridge.toml with new engines and models for some roles; everything else, comments included, unchanged.

    A role keeps its default effort only on its default engine and model (DEC-015)."""
    lines = text.splitlines()
    for role, (engine, model) in roles.items():
        default = ROLE_DEFAULTS[role]
        variant = default["variant"] if (engine, model) == (default["engine"], default["model"]) and engine == "claude-code" else None
        _set_key(lines, role, "engine", toml_str(engine))
        _set_key(lines, role, "model", toml_str(model))
        _set_key(lines, role, "variant", toml_str(variant) if variant else None)
    if opencode_port is not None:
        _activate_table(lines, "opencode")
        _set_key(lines, "opencode", "port", str(opencode_port))
    return "\n".join(lines) + "\n"
