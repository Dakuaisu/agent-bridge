"""agent-bridge: a planner, a supervisor and a builder agent, run against one repo."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_bridge import __version__, contract, owner, runtime
from agent_bridge.backends.claude_code import neutral_dir
from agent_bridge.clock import RealClock, iso
from agent_bridge.config import CONFIG_NAME, ENGINES, ROLES, ConfigError, load_config, render_config_template
from agent_bridge.engine import State
from agent_bridge.journal import read_events
from agent_bridge.live import format_seconds, render_event
from agent_bridge.planner import PlanError
from agent_bridge.protocol import display_token
from agent_bridge.report import review_entries, write_report
from agent_bridge.statedir import LockHeld, StateDir, atomic_write_json, atomic_write_text, lock_holder, read_json

EXIT_OK, EXIT_USAGE, EXIT_PAUSED = 0, 2, 3


# ------------------------------------------------------------------ helpers


def now() -> datetime:
    return RealClock().now()


def read_text_arg(value: str) -> str:
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def parse_role(value: str) -> tuple[str, str]:
    engine, sep, model = value.partition(":")
    if not sep or engine not in ENGINES or not model:
        raise runtime.UsageError(f"{value!r}: use ENGINE:MODEL with ENGINE one of {', '.join(ENGINES)}")
    return engine, model


def roles_from(a: argparse.Namespace) -> dict[str, tuple[str, str]]:
    return {role: parse_role(getattr(a, role)) for role in ROLES if getattr(a, role, None)}


def pick_port(start: int = 4100, end: int = 4200) -> int:
    for port in range(start, end):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise runtime.UsageError(f"no free port between {start} and {end}; pass --port")


def load_project(a: argparse.Namespace) -> tuple[Path, Any, StateDir]:
    repo = runtime.find_repo(a.repo)
    cfg = load_config(runtime.config_path(repo, a.config))
    return cfg.project.repo, cfg, StateDir(cfg.project.repo)


def run_mode(a: argparse.Namespace, default_forever: bool = True) -> int | None:
    """Exchanges to run: None means forever."""
    if getattr(a, "loop", None):
        return a.loop
    if getattr(a, "forever", False) or default_forever:
        return None
    return 1


def read_answers() -> str:
    print("\nYour answers (finish with an empty line; an empty answer means: use your recommendations):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines) or "use your recommendations"


def ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def confirm_each(sd: StateDir):
    def confirm(verdict: str, reply: str) -> str | None:
        print("=" * 70 + f"\nVERDICT: {verdict}\n" + "-" * 70 + f"\n{reply}\n" + "=" * 70)
        choice = input("[enter] send   e edit   n discard > ").strip().lower()
        if choice == "n":
            return None
        if choice == "e":
            draft = sd.root / "draft.md"
            atomic_write_text(draft, reply)
            subprocess.run([os.environ.get("EDITOR", "nano"), str(draft)])
            text = draft.read_text(encoding="utf-8")
            return text if text.strip() else None
        return reply

    return confirm


def converse(engine, exchanges: int | None) -> int:
    """Run, and while a terminal is attached answer the planner and approve the plan inline."""
    code = engine.run(exchanges=exchanges)
    while sys.stdin.isatty():
        if engine.st.phase == "INTERVIEW":
            engine.plan_answer(read_answers())
        elif engine.st.phase == "PLAN_REVIEW":
            if not ask("\nApprove the plan and start building? [y/N] "):
                print("Not approved. Edit the files if you like, then `agent-bridge approve`.")
                return EXIT_PAUSED
            for note in engine.approve_plan():
                print(note)
        else:
            break
        code = engine.run(exchanges=exchanges)
    return code


def start(engine, a: argparse.Namespace, argv: list[str], body) -> int:
    fatal, warnings = runtime.preflight(engine)
    for w in warnings:
        engine.j.console(f"warning: {w}")
    if fatal:
        for problem in fatal:
            engine.j.console(f"cannot start: {problem}")
        return EXIT_PAUSED if any("login" in p for p in fatal) else EXIT_USAGE
    for line in runtime.banner(engine):
        engine.j.console(line)
    return runtime.run_locked(engine, argv, body)


# ------------------------------------------------------------------ commands


def cmd_init(a: argparse.Namespace) -> int:
    repo = runtime.find_repo(a.repo)
    path = runtime.config_path(repo, a.config)
    if path.exists():
        raise runtime.UsageError(f"{path} already exists; init never overwrites it")
    roles = roles_from(a)
    exists = lambda rel: (repo / rel).exists()  # noqa: E731
    decisions = "docs/TRADEOFFS.md" if exists("docs/TRADEOFFS.md") and not exists("docs/DECISIONS.md") else "docs/DECISIONS.md"
    uses_opencode = any(engine == "opencode" for engine, _ in roles.values())
    port = a.port or (pick_port() if uses_opencode else None)
    text = render_config_template(
        name=a.name or repo.name,
        created=f"{now():%Y-%m-%d %H:%M}",
        decisions=decisions,
        worklog="docs/WORKLOG.md" if exists("docs/WORKLOG.md") else None,
        roles=roles,
        opencode_port=port,
    )
    atomic_write_text(path, text)
    cfg = load_config(path)
    print(f"wrote {path}")
    for label, p in (("PRD", cfg.project.prd), ("rules", cfg.project.rules[0]), ("ledger", cfg.project.decisions), ("open items", cfg.project.open_items)):
        print(f"  {label:<11} {p.relative_to(repo)}: {'found' if p.exists() else 'missing'}")
    if contract.ensure_gitignore(repo):
        print("added .bridge/ to .gitignore")
    agents = repo / "AGENTS.md"
    if not agents.exists() and not agents.is_symlink():
        print("suggestion: ln -s CLAUDE.md AGENTS.md  (opencode reads AGENTS.md; Claude Code reads CLAUDE.md)")
    warning = runtime.version_warning("opencode", cfg.opencode.accept) if not cfg.uses_opencode() else None
    if warning:
        print(f"warning: {warning}")
    if a.write_rules:
        before, after = runtime.rules_block_diff(cfg)
        sys.stdout.writelines(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "a/CLAUDE.md", "b/CLAUDE.md"))
        atomic_write_text(cfg.project.rules[0], after)
        print(f"\nadded the agent-bridge block to {cfg.project.rules[0].relative_to(repo)}")
    if a.adopt_legacy:
        sd = StateDir(repo)
        sd.ensure()
        state = State.load(sd.state)
        for note in runtime.import_legacy(sd, cfg, state):
            print(f"legacy: {note}")
        state.save(sd.state)
    print("\nnext: `agent-bridge check`, then `agent-bridge approve` to adopt the PRD and rules as the contract")
    return EXIT_OK


def cmd_new(a: argparse.Namespace) -> int:
    repo = Path(a.repo or os.getcwd()).resolve()
    repo.mkdir(parents=True, exist_ok=True)
    if runtime.Repo(repo).toplevel() != repo:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        print(f"initialised a git repo at {repo}")
    path = runtime.config_path(repo, a.config)
    taken = [p for p in ("docs/PRD.md", "CLAUDE.md", "docs/DECISIONS.md", "docs/OPEN.md") if (repo / p).exists()] + ([path.name] if path.exists() else [])
    if taken:
        raise runtime.UsageError(f"{', '.join(taken)} already exist; adopt them with `init` and `approve`, or change them with `decide`")
    if a.background and not a.auto_approve:
        raise runtime.UsageError("--background needs --auto-approve: otherwise the planner's interview needs you")
    roles = roles_from(a)
    port = a.port or (pick_port() if any(e == "opencode" for e, _ in roles.values()) else None)
    atomic_write_text(path, render_config_template(name=a.name or repo.name, created=f"{now():%Y-%m-%d %H:%M}", roles=roles, opencode_port=port))
    cfg = load_config(path)
    sd = StateDir(repo)
    contract.ensure_gitignore(repo)
    idea = read_text_arg(a.idea)
    engine = runtime.build_engine(cfg, sd)
    if a.background:
        engine.plan_new(idea, auto_approve=True)
        pid = runtime.spawn_background(repo, ["run", "--forever", "--repo", str(repo)], sd)
        print(f"planning and building in the background (pid {pid}); watch with `agent-bridge logs -f`")
        return EXIT_OK

    def body() -> int:
        engine.plan_new(idea, auto_approve=a.auto_approve)
        return converse(engine, None)

    return start(engine, a, ["new", idea[:60]], body)


def cmd_approve(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    holder = lock_holder(sd.lock)
    ids = list(a.ids or []) + ([a.reject] if a.reject else [])
    if ids:
        if holder:
            sd.inbox_put("approve", {"ids": ids, "reject": bool(a.reject), "reason": a.reason or ""}, iso(now()))
            print(f"queued for the running bridge (pid {holder.get('pid')}); applied at its next step boundary")
            return EXIT_OK
        engine = runtime.build_engine(cfg, sd, live=False)
        for line in runtime.run_locked(engine, ["approve", *ids], lambda: engine.approve_changes(ids, reject=bool(a.reject), reason=a.reason or "")) or []:
            print(line)
        return EXIT_OK
    if holder:
        raise runtime.UsageError(f"a bridge is running (pid {holder.get('pid')}); stop it before approving the plan")
    engine = runtime.build_engine(cfg, sd)
    st = engine.st
    if st.phase == "PLAN_REVIEW":
        def approve_and_run() -> int:
            for note in engine.approve_plan():
                print(note)
            return EXIT_OK if a.no_run or a.background else converse(engine, run_mode(a))

        code = start(engine, a, ["approve"], approve_and_run)
    elif st.contract is None:
        def adopt() -> int:
            for note in engine.approve_plan(adopt=True):
                print(note)
            if a.no_run or a.background or st.phase not in ("BUILDER_TURN", "SUPERVISOR_TURN", "WAITING"):
                print("contract adopted; start with `agent-bridge run --kickoff \"...\" --forever`")
                return EXIT_OK
            return engine.run(exchanges=run_mode(a))

        code = start(engine, a, ["approve", "--adopt"], adopt)
    elif contract.drifted(cfg, st.contract.get("hashes", {})):
        def reapprove() -> int:
            for note in engine.reapprove_contract():
                print(note)
            return EXIT_OK

        code = runtime.run_locked(engine, ["approve"], reapprove)
    else:
        waiting = engine.waiting_changes()
        if waiting:
            print("plan changes waiting for you (approve by id, or --reject ID --reason ...):")
            for c in waiting:
                print(f"  {c['id']} {c['title']} (affects {', '.join(display_token(t) for t in c['affects'])}; diff {c['diff']})")
        else:
            print("nothing to approve: the contract is approved and unchanged")
        return EXIT_OK
    if a.background and code == EXIT_OK:
        pid = runtime.spawn_background(repo, ["run", "--forever", "--repo", str(repo)], sd)
        print(f"running in the background (pid {pid})")
    return code


def cmd_decide(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    doc = Path(a.doc).resolve()
    if not doc.exists():
        raise runtime.UsageError(f"{doc} does not exist")
    holder = lock_holder(sd.lock)
    if holder:
        sd.inbox_put("decide", {"doc": str(doc), "auto_approve": a.auto_approve}, iso(now()))
        print(f"queued for the running bridge (pid {holder.get('pid')}); the planner takes it at the next step boundary")
        return EXIT_OK
    engine = runtime.build_engine(cfg, sd)
    if engine.st.contract is None:
        raise runtime.UsageError("there is no approved contract yet; `agent-bridge approve` first")
    engine.decide(doc, auto_approve=a.auto_approve)
    if a.no_run:
        print("decisions queued; `agent-bridge run --forever` applies them")
        return EXIT_OK
    if a.background:
        pid = runtime.spawn_background(repo, ["run", "--forever", "--repo", str(repo)], sd)
        print(f"applying the decisions and running in the background (pid {pid})")
        return EXIT_OK
    return start(engine, a, ["decide", str(doc)], lambda: converse(engine, run_mode(a)))


def cmd_run(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    if a.background:
        argv = [x for x in sys.argv[1:] if x != "--background"] if a._argv is None else [x for x in a._argv if x != "--background"]
        pid = runtime.spawn_background(repo, argv + ["--repo", str(repo)], sd)
        print(f"running in the background (pid {pid}); watch with `agent-bridge logs -f`, stop with `agent-bridge stop`")
        return EXIT_OK
    engine = runtime.build_engine(cfg, sd, echo=not runtime.in_background(), confirm=confirm_each(sd) if a.confirm_each else None)

    def body() -> int:
        if not engine.can_run():
            return engine.run()
        if a.new_supervisor:
            engine.st.rotate["supervisor"] = "--new-supervisor"
        if a.new_builder:
            engine.st.rotate["builder"] = "--new-builder"
        if a.kickoff:
            engine.kickoff(read_text_arg(a.kickoff))
        engine.save()
        return converse(engine, None if a.forever else (a.loop or 1))

    return start(engine, a, a._argv or ["run"], body)


def cmd_status(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    info = status_info(cfg, sd)
    if a.json:
        print(json.dumps(info, indent=2, default=str))
    else:
        print(format_status(info))
    return EXIT_OK


def status_info(cfg, sd: StateDir) -> dict[str, Any]:
    st = State.load(sd.state)
    holder = lock_holder(sd.lock)
    events = read_events(sd.events)
    open_turn = None
    for e in events:
        if e["kind"] == "turn_start":
            open_turn = e
        elif e["kind"] in ("turn_end", "turn_error") and open_turn and e.get("role") == open_turn.get("role"):
            open_turn = None
    last_launch = next((e["ts"] for e in reversed(events) if e["kind"] == "launch"), None)
    warnings = review_entries(sd.review_log, last_launch)
    backends = runtime._factory(cfg, sd)
    roles = {}
    for role in ROLES:
        b = backends[role]
        session = (st.sessions.get(role) or {})
        roles[role] = {
            "engine": b.engine,
            "model": b.cfg.engine_model(),
            "read_only": b.capabilities().read_only,
            "session": None if session.get("closed") else session.get("id"),
        }
    changes = [read_json(p) for p in sorted((sd.plan / "changes").glob("PC-*.json"))]
    waiting = [c for c in changes if isinstance(c, dict) and c.get("status") == "awaiting"]
    drift = contract.drifted(cfg, st.contract.get("hashes", {})) if st.contract else []
    t = now()
    turn = None
    if open_turn and holder:
        started = datetime.fromisoformat(open_turn["ts"])
        last = datetime.fromisoformat(events[-1]["ts"])
        turn = {"role": open_turn.get("role"), "running_s": (t - started).total_seconds(), "last_event_s": (t - last).total_seconds()}
    return {
        "project": cfg.project.name,
        "repo": str(cfg.project.repo),
        "running": holder,
        "phase": st.phase,
        "exchange": st.exchange,
        "pause": st.pause,
        "wait": (st.wait or {}).get("active"),
        "sleep": st.sleep,
        "turn": turn,
        "last_verdict": st.last_verdict,
        "roles": roles,
        "contract": {"approved_at": st.contract.get("approved_at"), "by": st.contract.get("by"), "drift": drift} if st.contract else None,
        "waiting_changes": [{"id": c["id"], "title": c["title"], "affects": c["affects"]} for c in waiting],
        "paused_phases": st.blocked.get("phases", {}),
        "owner_queue": len(st.owner_queue) + len(sd.inbox_peek()),
        "decisions_needed": (st.review or {}).get("decisions", []),
        "unchanged_streak": st.counters.get("unchanged", 0),
        "max_unchanged": cfg.budget.max_unchanged_exchanges,
        "warnings_since_launch": [f"{ts} {title}" for ts, title in warnings],
        "phases_done": st.phases_done,
    }


def format_status(i: dict[str, Any]) -> str:
    out = [f"agent-bridge status: {i['project']} ({i['repo']})"]
    run = i["running"]
    out.append(f"running:      {'yes, pid ' + str(run.get('pid')) + ' since ' + str(run.get('started')) if run else 'no'}")
    state = f"{i['phase']} (exchange {i['exchange']})"
    if i["pause"]:
        state += f"; paused ({i['pause']['reason']}): {i['pause']['detail']}"
    if i["wait"]:
        w = i["wait"]
        spec = w["spec"]
        state += f"; waiting: {spec['kind']} {spec['target']} (asked by the {w['source']}; ends by {w['deadline']})"
    if i["sleep"]:
        state += f"; sleeping until {i['sleep']['until']} ({i['sleep']['reason']})"
    out.append(f"state:        {state}")
    if i["turn"]:
        t = i["turn"]
        out.append(f"turn:         {t['role']} running for {format_seconds(t['running_s'])}; last event {format_seconds(t['last_event_s'])} ago")
    if i["last_verdict"]:
        out.append(f"last verdict: (exchange {i['last_verdict']['exchange']}) {i['last_verdict']['text']}")
    for role, r in i["roles"].items():
        out.append(f"{role + ':':<13} {r['engine']} {r['model']}; read-only: {r['read_only']}; session {r['session'] or '(new on the next turn)'}")
    c = i["contract"]
    out.append(f"contract:     {'approved ' + str(c['approved_at']) + ' by ' + str(c['by']) if c else 'not approved'}" + (f"; changed since: {', '.join(c['drift'])}" if c and c["drift"] else ""))
    for w in i["waiting_changes"]:
        out.append(f"waiting:      {w['id']} {w['title']} (blocks {', '.join(display_token(t) for t in w['affects'])})")
    for token, reason in i["paused_phases"].items():
        out.append(f"paused phase: {display_token(token)} ({reason})")
    for done in i["phases_done"]:
        out.append(f"phase done:   {done['phase']} (exchange {done['exchange']})")
    if i["decisions_needed"]:
        out.append("decisions:    " + "; ".join(i["decisions_needed"]))
    out.append(f"owner queue:  {i['owner_queue']} message(s) not yet delivered")
    out.append(f"budget:       unchanged streak {i['unchanged_streak']}/{i['max_unchanged'] or 'off'}")
    warnings = i["warnings_since_launch"]
    out.append(f"warnings:     {len(warnings)} since the last launch" + ("".join(f"\n  - {w}" for w in warnings[-5:]) if warnings else ""))
    return "\n".join(out)


def cmd_stop(a: argparse.Namespace) -> int:
    _, _, sd = load_project(a)
    holder = lock_holder(sd.lock)
    if not holder:
        print("no bridge is running on this repo; nothing to stop")
        return EXIT_OK
    sd.request_stop(now=a.now)
    how = "aborts the running turn and stops" if a.now else "stops at the next step boundary (the running turn finishes first)"
    print(f"STOP requested: the bridge (pid {holder.get('pid')}) {how}; any unsent message is saved to {sd.unsent}")
    return EXIT_OK


def cmd_pin(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    holder = lock_holder(sd.lock)
    if holder:
        raise runtime.UsageError(f"a bridge is running (pid {holder.get('pid')}); stop it before changing sessions")
    sd.ensure()
    st = State.load(sd.state)
    backends = runtime._factory(cfg, sd)
    changed = False
    for role in ROLES:
        if getattr(a, f"new_{role}"):
            info = st.sessions.get(role) or {}
            st.sessions[role] = {**info, "closed": True}
            st.rotate.pop(role, None)
            print(f"{role}: the next turn starts a fresh session")
            changed = True
        sid = getattr(a, role)
        if not sid:
            continue
        backend = backends[role]
        directory = backend.session_directory(sid)
        expected = {str(repo)}
        if backend.engine == "claude-code" and role != "builder":
            expected.add(str(neutral_dir(cfg.project.name, repo, role)))
        if directory is None and not a.force:
            raise runtime.UsageError(f"{role}: cannot find session {sid} to check that it belongs to {repo}; pass --force to pin it anyway")
        if directory is not None and directory not in expected:
            raise runtime.UsageError(f"{role}: session {sid} belongs to {directory}, not this repo; refusing to pin it")
        st.sessions[role] = {"engine": backend.engine, "id": sid, "started": iso(now()), "closed": False, "adopted": True}
        registry = read_json(sd.sessions, default=[]) or []
        registry.append({"role": role, "engine": backend.engine, "id": sid, "title": "(pinned)", "directory": directory, "created": iso(now()), "retired": None, "adopted": True})
        atomic_write_json(sd.sessions, registry)
        print(f"{role}: pinned {sid}" + (f" ({directory})" if directory else " (not verified: --force)"))
        if backend.engine == "opencode":
            print("  note: a session created in the opencode TUI lacks the question deny that `opencode run` adds; the bridge's question polling still catches questions")
        changed = True
    if changed:
        st.save(sd.state)
    else:
        print("nothing to do: pass --builder ID, --supervisor ID, --planner ID or --new-<role>")
    return EXIT_OK


def cmd_say(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    text = read_text_arg(a.message)
    holder = lock_holder(sd.lock)
    st = State.load(sd.state)
    if st.phase == "INTERVIEW" and not holder:
        engine = runtime.build_engine(cfg, sd)
        engine.plan_answer(text)
        return start(engine, a, ["say", "(answers)"], lambda: converse(engine, None))
    sd.ensure()
    sd.inbox_put("say", {"text": text, "to": a.to}, iso(now()))
    if holder:
        print(f"queued for the running bridge (pid {holder.get('pid')}): delivered at the next step boundary; it ends a wait")
    else:
        print("queued: delivered when the bridge next runs (`agent-bridge run --forever`)")
    return EXIT_OK


def cmd_review(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    sd.ensure()
    changes = [read_json(p) for p in sorted((sd.plan / "changes").glob("PC-*.json"))]
    found = owner.collect(cfg.project.decisions, [cfg.project.open_items], [c for c in changes if isinstance(c, dict)])
    text = owner.render_todo(cfg.project.name, repo, found, now())
    atomic_write_text(sd.owner_todo, text)
    print(text)
    if a.template:
        target = (repo / a.template).resolve()
        if target.exists():
            raise runtime.UsageError(f"{target} exists; review never overwrites it")
        atomic_write_text(target, owner.review_template(repo, found, now()))
        print(f"wrote {target}: write your decisions, then `agent-bridge decide {a.template}`")
    return EXIT_OK


def cmd_report(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    sd.ensure()
    path = write_report(cfg, sd, State.load(sd.state), now=now(), out=Path(a.out).resolve() if a.out else None)
    print(f"wrote {path}")
    return EXIT_OK


def cmd_logs(a: argparse.Namespace) -> int:
    _, _, sd = load_project(a)
    target = {"transcript": sd.loop_log, "review": sd.review_log, "console": sd.console_log, "serve": sd.serve_log, "events": sd.events}
    chosen = next((k for k in target if getattr(a, k)), None)
    if chosen is None:
        records = read_events(sd.events)
        for e in records[-a.n :]:
            line = render_event(e)
            if line:
                print(line)
        if a.follow:
            _follow(sd.events, rendered=True)
        return EXIT_OK
    path = target[chosen]
    if path.exists():
        print("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-a.n :]))
    if a.follow:
        _follow(path, rendered=False)
    return EXIT_OK


def _follow(path: Path, *, rendered: bool) -> None:
    offset = path.stat().st_size if path.exists() else 0
    try:
        while True:
            time.sleep(1)
            if not path.exists() or path.stat().st_size <= offset:
                continue
            with path.open(encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
                offset = f.tell()
            for line in chunk.splitlines():
                if rendered:
                    try:
                        out = render_event(json.loads(line))
                    except json.JSONDecodeError:
                        out = None
                    if out:
                        print(out, flush=True)
                else:
                    print(line, flush=True)
    except KeyboardInterrupt:
        return


def cmd_check(a: argparse.Namespace) -> int:
    repo, cfg, sd = load_project(a)
    print(f"config:       {cfg.path} (valid)")
    engine = runtime.build_engine(cfg, sd, live=False, echo=False)
    fatal, warnings = runtime.preflight(engine)
    for role in ROLES:
        b = engine.backends[role]
        try:
            version = b.version_check()
        except Exception as e:  # noqa: BLE001 - reported, not raised
            version = f"unavailable ({e})"
        print(f"{role + ':':<13} {b.engine} {b.cfg.engine_model()} ({version}); read-only: {b.capabilities().read_only}")
    name, email = engine.repo.local_identity()
    print(f"git identity: {name or '(global)'} <{email or 'not set locally'}>")
    ignored = ".bridge/" in (repo / ".gitignore").read_text() if (repo / ".gitignore").exists() else False
    print(f".gitignore:   {'ignores .bridge/' if ignored else 'does not ignore .bridge/ (init adds it)'}")
    agents = repo / "AGENTS.md"
    print(f"AGENTS.md:    {'symlink to ' + os.readlink(agents) if agents.is_symlink() else 'regular file' if agents.exists() else 'missing'}")
    for label, p in (("PRD", cfg.project.prd), ("rules", cfg.project.rules[0]), ("ledger", cfg.project.decisions), ("open items", cfg.project.open_items), ("results", cfg.project.results)):
        print(f"{label + ':':<13} {p.relative_to(repo)} {'(present)' if p.exists() else '(missing)'}")
    st = engine.st
    if st.contract:
        drift = contract.drifted(cfg, st.contract.get("hashes", {}))
        print(f"contract:     approved {st.contract.get('approved_at')}" + (f"; changed since: {', '.join(drift)}" if drift else "; unchanged"))
    else:
        print("contract:     not approved")
    holder = lock_holder(sd.lock)
    print(f"running:      {'pid ' + str(holder.get('pid')) if holder else 'no'}")
    key = "set; stripped from every child" if cfg.billing_mode == "subscription" else "kept (billing.mode = api-key)"
    print(f"API key:      ANTHROPIC_API_KEY {key if os.environ.get('ANTHROPIC_API_KEY') else 'not set'}")
    for w in warnings:
        print(f"warning:      {w}")
    for f in fatal:
        print(f"PROBLEM:      {f}")
    return EXIT_OK if not fatal else (EXIT_PAUSED if any("login" in f for f in fatal) else EXIT_USAGE)


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"agent-bridge {__version__}")
    sub = parser.add_subparsers(dest="command")

    def command(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--repo", type=Path, help="the project repo (default: the current git repo)")
        p.add_argument("--config", type=Path, help=f"the config file (default: <repo>/{CONFIG_NAME})")
        p.set_defaults(func=func)
        return p

    def roles(p: argparse.ArgumentParser) -> None:
        for role in ROLES:
            p.add_argument(f"--{role}", metavar="ENGINE:MODEL", help=f"engine and model for the {role}")
        p.add_argument("--port", type=int, help="opencode server port, when a role uses opencode")

    def mode(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--forever", action="store_true", help="run until PROJECT COMPLETE (default)")
        g.add_argument("--loop", type=int, metavar="N", help="run N exchanges")
        g.add_argument("--background", action="store_true", help="run detached; watch with `logs -f`")
        g.add_argument("--no-run", action="store_true", help="only record; do not start the loop")

    p = command("init", cmd_init, "write bridge.toml for an existing repo")
    roles(p)
    p.add_argument("--name", help="project name (default: the repo folder)")
    p.add_argument("--write-rules", action="store_true", help="add the agent-bridge block to CLAUDE.md (shows the diff)")
    p.add_argument("--adopt-legacy", action="store_true", help="adopt an old tools/bridge.py .bridge/ folder")

    p = command("new", cmd_new, "plan a new project with the planner, then build it")
    p.add_argument("idea", help='the idea, or @file')
    roles(p)
    p.add_argument("--name", help="project name")
    p.add_argument("--auto-approve", action="store_true", help="use the planner's recommendations and approve the plan; every choice is logged for review")
    p.add_argument("--background", action="store_true", help="with --auto-approve: plan and build detached")

    p = command("approve", cmd_approve, "approve the plan, adopt an existing contract, or settle waiting plan changes")
    p.add_argument("ids", nargs="*", metavar="PC-n", help="plan changes to approve")
    p.add_argument("--reject", metavar="PC-n", help="reject a waiting plan change")
    p.add_argument("--reason", help="why (with --reject)")
    mode(p)

    p = command("decide", cmd_decide, "apply an owner decisions doc (D-items and a Done when list) through the planner")
    p.add_argument("doc", help="for example docs/OWNER_REVIEW.md")
    p.add_argument("--auto-approve", action="store_true", help="answer the planner's questions with its recommendations")
    mode(p)

    p = command("run", cmd_run, "run the loop on the approved contract")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--forever", action="store_true", help="run until PROJECT COMPLETE or a pause")
    g.add_argument("--loop", type=int, metavar="N", help="run N exchanges (default: 1)")
    p.add_argument("--kickoff", metavar="MSG|@file", help="an owner message for the builder (the supervisor sees it too)")
    p.add_argument("--new-supervisor", action="store_true", help="start a fresh supervisor session")
    p.add_argument("--new-builder", action="store_true", help="start a fresh builder session")
    p.add_argument("--confirm-each", action="store_true", help="show each supervisor reply for send / edit / discard")
    p.add_argument("--background", action="store_true", help="run detached; watch with `logs -f`")

    p = command("status", cmd_status, "what the bridge is doing")
    p.add_argument("--json", action="store_true")

    p = command("stop", cmd_stop, "stop the running bridge at the next step boundary")
    p.add_argument("--now", action="store_true", help="abort the running turn too")

    p = command("pin", cmd_pin, "adopt a session for a role, or start fresh ones")
    for role in ROLES:
        p.add_argument(f"--{role}", metavar="SESSION_ID")
        p.add_argument(f"--new-{role}", action="store_true")
    p.add_argument("--force", action="store_true", help="pin even if the session's directory cannot be checked")

    p = command("say", cmd_say, "send an owner message (or the interview answers)")
    p.add_argument("message", help="the message, or @file")
    p.add_argument("--to", choices=("both", "builder", "supervisor"), default="both")

    p = command("review", cmd_review, "the owner to-do: decisions to review, OWNER-BLOCKED items, waiting plan changes")
    p.add_argument("--template", metavar="PATH", help="also write an OWNER_REVIEW.md skeleton")

    p = command("report", cmd_report, "write the owner-review report")
    p.add_argument("--out", help="write to this path instead of .bridge/reports/")

    p = command("logs", cmd_logs, "show the logs")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=50, help="lines or events to show")
    for name in ("transcript", "review", "console", "serve", "events"):
        p.add_argument(f"--{name}", action="store_true")

    command("check", cmd_check, "check the setup without any model call")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._argv = list(argv) if argv is not None else None
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except (runtime.UsageError, ConfigError, PlanError) as e:
        print(f"agent-bridge: {e}", file=sys.stderr)
        return EXIT_USAGE
    except LockHeld as e:
        print(f"agent-bridge: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrupted; the state is saved and `agent-bridge run` resumes it", file=sys.stderr)
        return 130
