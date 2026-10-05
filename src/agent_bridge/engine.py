"""The loop: a persisted state machine (docs/DESIGN.md section 7).

Every transition is saved to .bridge/state.json before the action it leads to, so a crash or a
restart resumes where it stopped. Every message an agent receives is composed of labelled blocks
and recorded in loop.log exactly as delivered.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_bridge import prompts
from agent_bridge.backends.base import (
    AuthFailed,
    Backend,
    BackendError,
    Event,
    Reply,
    Unsupported,
)
from agent_bridge.clock import Clock, iso
from agent_bridge.config import Config
from agent_bridge.journal import Journal
from agent_bridge.protocol import Block, SupervisorOutput, parse_builder, parse_supervisor, render
from agent_bridge.repo import Repo, Snapshot, attribution_problems
from agent_bridge.statedir import StateDir, atomic_write_json, atomic_write_text, read_json

REPORT_CHARS = 24_000
EXIT_OK, EXIT_UNSUPPORTED, EXIT_PAUSED = 0, 2, 3

DEFAULT_DANGER = [
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b)"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\s+(?:/|~|\$HOME)"), "rm -rf on an absolute or home path"),
    (re.compile(r"(?i)\bdrop\s+(?:table|database|schema)\b"), "DROP TABLE / DATABASE"),
    (re.compile(r"(?i)co-authored-by"), "Co-Authored-By in a command"),
]
PUSH = re.compile(r"\bgit\s+push\b")
SHELL_TOOLS = {"bash", "shell"}


class FatalError(Exception):
    """Unsupported version, flags or billing: stop with the fix, never retry."""


@dataclass
class State:
    schema: int = 1
    phase: str = "IDLE"
    exchange: int = 0
    pending: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    wait: dict[str, Any] | None = None
    sleep: dict[str, Any] | None = None
    pause: dict[str, Any] | None = None
    replan: dict[str, Any] | None = None
    sessions: dict[str, Any] = field(default_factory=dict)
    rotate: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    feed: list[dict[str, str]] = field(default_factory=list)
    owner_queue: list[dict[str, Any]] = field(default_factory=list)
    supervisor_head: str | None = None
    contract: dict[str, Any] | None = None
    blocked: dict[str, Any] = field(default_factory=lambda: {"changes": {}, "phases": {}})
    replans: dict[str, int] = field(default_factory=dict)
    phases_done: list[dict[str, Any]] = field(default_factory=list)
    last_verdict: dict[str, Any] | None = None
    complete: dict[str, Any] | None = None
    planning: dict[str, Any] | None = None
    handoff: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        atomic_write_json(path, asdict(self))


def new_pending(**kw: Any) -> dict[str, Any]:
    return {"supervisor": None, "supervisor_note": "", "planner": [], "notes": [], "attempt": 0, "in_flight": False, **kw}


class Engine:
    def __init__(
        self,
        cfg: Config,
        sd: StateDir,
        journal: Journal,
        backends: dict[str, Backend],
        *,
        clock: Clock,
        repo: Repo | None = None,
        confirm: Callable[[str, str], str | None] | None = None,
        on_complete: Callable[[Engine], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.sd = sd
        self.j = journal
        self.backends = backends
        self.clock = clock
        self.repo = repo or Repo(cfg.project.repo)
        self.confirm = confirm
        self.on_complete = on_complete
        self.st = State.load(sd.state)
        self.enforcement = {role: b.capabilities().read_only for role, b in backends.items()}
        self._replies_this_run = 0
        self._exchange_limit: int | None = None
        self._restore_sessions()

    # ------------------------------------------------------------------ state

    def save(self) -> None:
        self.st.save(self.sd.state)

    def now(self) -> datetime:
        return self.clock.now()

    def _restore_sessions(self) -> None:
        for role, backend in self.backends.items():
            info = self.st.sessions.get(role) or {}
            if info.get("id") and not info.get("closed"):
                backend.resume(info["id"])
            else:
                backend.start_session(self._title(role))

    def _title(self, role: str) -> str:
        return f"{self.cfg.project.name} {role} {self.now():%Y-%m-%d %H:%M}"

    def _record_session(self, role: str, session_id: str) -> None:
        info = self.st.sessions.get(role) or {}
        if info.get("id") == session_id and not info.get("closed"):
            return
        backend = self.backends[role]
        self.st.sessions[role] = {"engine": backend.engine, "id": session_id, "started": iso(self.now()), "closed": False}
        registry = read_json(self.sd.sessions, default=[]) or []
        registry.append(
            {
                "role": role,
                "engine": backend.engine,
                "model": backend.cfg.engine_model(),
                "id": session_id,
                "title": backend._title,
                "directory": str(self.cfg.project.repo),
                "created": iso(self.now()),
                "retired": None,
            }
        )
        atomic_write_json(self.sd.sessions, registry)
        self.save()
        self.j.event("session", role=role, engine=backend.engine, id=session_id)

    def retire_session(self, role: str, reason: str) -> str | None:
        info = self.st.sessions.get(role) or {}
        old = info.get("id")
        if old:
            registry = read_json(self.sd.sessions, default=[]) or []
            for entry in registry:
                if entry.get("id") == old and entry.get("retired") is None:
                    entry["retired"] = iso(self.now())
                    entry["retired_reason"] = reason
            atomic_write_json(self.sd.sessions, registry)
        self.st.sessions[role] = {**info, "closed": True}
        self.backends[role].start_session(self._title(role))
        self.j.event("session_retired", role=role, id=old, reason=reason)
        return old

    # ------------------------------------------------------------------ owner input

    def kickoff(self, text: str, *, origin: str = "owner") -> None:
        """Queue a first message for the builder; the supervisor sees it in its next turn."""
        if self.st.phase == "COMPLETE" or (self.st.sessions.get("supervisor") or {}).get("closed"):
            self.st.rotate["supervisor"] = "new work after PROJECT COMPLETE"
        if origin == "owner":
            self.st.owner_queue.append({"text": text, "to_builder": True, "to_supervisor": True, "delivered": False, "shown": False, "at": iso(self.now())})
            pending = self.st.pending or new_pending()
        else:
            pending = self.st.pending or new_pending()
            pending["planner"] = [*pending["planner"], text]
        if self.st.phase == "SUPERVISOR_TURN":
            self.j.event("review_skipped", reason="a kickoff replaced the pending review", exchange=self.st.exchange)
            self.st.review = None
        self.st.pending = pending
        self.st.phase = "BUILDER_TURN"
        self.st.complete = None
        self.save()
        self.j.event("kickoff", origin=origin, text=text)

    # ------------------------------------------------------------------ the loop

    def run(self, *, exchanges: int | None = None) -> int:
        self._exchange_limit = exchanges
        self._replies_this_run = 0
        if self.st.phase == "PAUSED":
            self._resume_from_pause()
        self.j.event("run_start", phase=self.st.phase, exchange=self.st.exchange, limit=exchanges)
        try:
            while True:
                if self.sd.stop_requested():
                    return self._stop()
                phase = self.st.phase
                if phase == "BUILDER_TURN":
                    code = self._builder_turn()
                elif phase == "SUPERVISOR_TURN":
                    if self._exchange_limit is not None and self._replies_this_run >= self._exchange_limit:
                        self.j.console(f"Done: {self._replies_this_run} exchange(s). Next: the supervisor reviews the last report.")
                        return EXIT_OK
                    code = self._supervisor_turn()
                elif phase == "COMPLETE":
                    return EXIT_OK
                elif phase == "PAUSED":
                    return EXIT_PAUSED
                else:
                    self.j.console("Nothing to do: no kickoff, no pending message and no report to review.")
                    return EXIT_UNSUPPORTED
                if code is not None:
                    return code
        except FatalError as e:
            self.j.console(f"STOPPED: {e}")
            self.j.review("UNSUPPORTED: run stopped", str(e))
            return EXIT_UNSUPPORTED

    def _resume_from_pause(self) -> None:
        pause = self.st.pause or {}
        self.st.phase = pause.get("resume") or "IDLE"
        self.st.pause = None
        self.sd.clear_stop()
        self.sd.paused.unlink(missing_ok=True)
        self.save()
        self.j.event("resumed", reason=pause.get("reason"), phase=self.st.phase)

    # ------------------------------------------------------------------ builder

    def builder_message(self) -> tuple[str, list[Block]]:
        p = self.st.pending or new_pending()
        blocks = [Block("bridge", prompts.HEADLESS)]
        blocked = self.blocked_tokens()
        if blocked:
            blocks.append(Block("bridge", f"Blocked until the owner settles them: {', '.join(blocked)}. Do not work on them."))
        for item in self.st.owner_queue:
            if item["to_builder"] and not item["delivered"]:
                blocks.append(Block("owner", item["text"], note="(verbatim; binding)"))
        for text in p.get("planner", []):
            blocks.append(Block("planner", text))
        for note in p.get("notes", []):
            blocks.append(Block("bridge", note))
        if p.get("supervisor"):
            blocks.append(Block("supervisor", p["supervisor"], note=p.get("supervisor_note", "")))
        return render(blocks), blocks

    def blocked_tokens(self) -> list[str]:
        return []

    def _builder_turn(self) -> int | None:
        backend = self.backends["builder"]
        p = self.st.pending or new_pending()
        rotate_reason = self.st.rotate.pop("builder", None)
        if rotate_reason:
            old = self.retire_session("builder", rotate_reason)
            p["notes"] = [prompts.handoff_note(self.cfg, old, rotate_reason), *p.get("notes", [])]
        self.st.pending = p
        message, blocks = self.builder_message()
        p["attempt"] = p.get("attempt", 0) + 1
        p["in_flight"] = True
        self.save()
        n = self.st.exchange
        self.j.transcript(f"EXCHANGE {n} | to builder ({backend.describe()})", message)
        self.j.event("message", recipient="builder", exchange=n, blocks=[{"origin": b.origin, "note": b.note} for b in blocks])
        before = self.repo.snapshot()
        started = self.now()
        try:
            reply = self._send("builder", message)
        except BackendError as e:
            return self._turn_failed("builder", e)
        after = self.repo.snapshot()
        self._delivered(blocks)
        self._after_builder(reply, before, after, started)
        return None

    def _delivered(self, blocks: list[Block]) -> None:
        """Mark owner messages delivered, and keep what else the builder was told for the supervisor."""
        for item in self.st.owner_queue:
            if item["to_builder"] and not item["delivered"]:
                item["delivered"] = True
        for b in blocks:
            if b.origin == "planner":
                self.st.feed.append({"origin": "planner", "text": b.text})
            elif b.origin == "bridge" and b.text != prompts.HEADLESS and not b.text.startswith("Blocked until"):
                self.st.feed.append({"origin": "bridge", "text": b.text})
        self._prune_owner_queue()

    def _prune_owner_queue(self) -> None:
        self.st.owner_queue = [
            i for i in self.st.owner_queue if (i["to_builder"] and not i["delivered"]) or (i["to_supervisor"] and not i["shown"])
        ]

    def _after_builder(self, reply: Reply, before: Snapshot, after: Snapshot, started: datetime) -> None:
        cfg = self.cfg
        atomic_write_text(self.sd.builder_last, reply.text)
        report_path = self.sd.turns / f"{self.st.exchange:04d}-builder-report.md"
        atomic_write_text(report_path, reply.text)
        self.j.transcript(f"EXCHANGE {self.st.exchange} | builder report", reply.text)
        self._check_models("builder", reply)
        self._audit_builder(reply, before, after)
        signals = parse_builder(reply.text, now=self.now(), phase_pattern=cfg.rotation.phase_complete_pattern)
        changed = before.fingerprint() != after.fingerprint()
        self.st.counters["unchanged"] = 0 if changed else self.st.counters.get("unchanged", 0) + 1
        if reply.context_tokens and reply.context_tokens >= cfg.rotation.builder_max_context_tokens:
            self.st.rotate["builder"] = f"context reached {reply.context_tokens:,} tokens"
        self.st.review = {
            "report": str(report_path),
            "exchange": self.st.exchange,
            "duration_s": (self.now() - started).total_seconds(),
            "changed": changed,
            "wait": signals.wait.describe() if signals.wait else None,
            "wait_error": signals.wait_error,
            "decisions": signals.decisions_needed,
            "phase_claims": signals.phase_claims,
            "context_tokens": reply.context_tokens,
            "attempt": 0,
            "nudges": [],
        }
        self.st.pending = None
        self.st.phase = "SUPERVISOR_TURN"
        for key in ("timeouts_builder", "questions_builder", "errors_builder"):
            self.st.counters.pop(key, None)
        self.save()
        self.j.event(
            "builder_report",
            exchange=self.st.exchange,
            chars=len(reply.text),
            changed=changed,
            context_tokens=reply.context_tokens,
            decisions=len(signals.decisions_needed),
            wait=self.st.review["wait"],
        )

    def _audit_builder(self, reply: Reply, before: Snapshot, after: Snapshot) -> None:
        patterns = list(DEFAULT_DANGER) + [(p, "safety.danger_commands") for p in self.cfg.safety.danger_commands]
        if self.cfg.git_push == "never":
            patterns.append((PUSH, "git push while git.push = never"))
        for call in reply.tool_calls:
            if call.name.lower() not in SHELL_TOOLS:
                continue
            for pattern, label in patterns:
                if pattern.search(call.summary):
                    self.j.review(f"DANGER COMMAND ({label}) at exchange {self.st.exchange}", call.summary)
        _, email = self.repo.local_identity()
        for commit in self.repo.new_commits(before.head, after.head):
            problems = attribution_problems(commit)
            if problems:
                self.j.review(f"COMMIT ATTRIBUTION in {commit.short}", f"{commit.subject}\n" + "\n".join(problems))
            if email and commit.author_email != email:
                self.j.review(
                    f"COMMIT AUTHOR in {commit.short}",
                    f"author {commit.author_name} <{commit.author_email}> differs from the repo's local user.email <{email}>",
                )
        if self.cfg.git_push == "never" and before.remotes != after.remotes:
            moved = sorted(set(before.remotes.items()) ^ set(after.remotes.items()))
            self.j.review("REMOTE REFS MOVED while git.push = never (a push or a fetch)", "\n".join(f"{r} {s}" for r, s in moved))

    def _check_models(self, role: str, reply: Reply) -> None:
        expected = self.backends[role].cfg.engine_model().split("/")[-1]
        served = {m for m in reply.served_models if m}
        if not served:
            return
        wrong = sorted(m for m in served if not (m == expected or (not re.search(r"\d", expected) and expected in m)))
        if wrong:
            self.j.review(
                f"MODEL MISMATCH: {role} was served by {', '.join(wrong)}, not {expected}",
                f"session {reply.session_id}, exchange {self.st.exchange}",
            )

    # ------------------------------------------------------------------ supervisor

    def supervisor_message(self, *, new_session: bool) -> tuple[str, list[Block]]:
        cfg, st = self.cfg, self.st
        review = st.review or {}
        enforcement = self.enforcement["supervisor"]
        blocks: list[Block] = []
        if new_session:
            blocks.append(Block("bridge", prompts.supervisor_role(cfg, enforcement, self._supervisor_rules_text())))
            reason = st.handoff.get("supervisor")
            if st.exchange > 1 and st.last_verdict:
                blocks.append(Block("bridge", prompts.supervisor_handoff(st.exchange - 1, st.last_verdict.get("text"), reason or "a new session")))
        else:
            blocks.append(Block("bridge", prompts.supervisor_reminder(cfg, enforcement)))
        blocks.append(Block("bridge", prompts.REVERIFY))
        blocks.append(Block("bridge", self._since_text(review)))
        if review.get("wait"):
            blocks.append(Block("bridge", f"The builder asked for: {review['wait']}. It is honoured after your reply unless you write NO WAIT or a different WAIT line."))
        if review.get("wait_error"):
            blocks.append(Block("bridge", f"The builder wrote a WAIT line the bridge could not use: {review['wait_error']}"))
        if review.get("decisions"):
            blocks.append(Block("bridge", f"The builder listed {len(review['decisions'])} DECISIONS NEEDED; answer each in your REPLY."))
        for claim in review.get("phase_claims", [])[:1]:
            blocks.append(Block("bridge", f'The builder says "{claim}". If you verify that phase\'s exit criteria, write PHASE COMPLETE: <phase>.'))
        unchanged = st.counters.get("unchanged", 0)
        if unchanged >= 3:
            blocks.append(Block("bridge", f"The last {unchanged} exchanges changed nothing in the repo. If the builder is waiting for a job or a time, use a WAIT directive instead of acknowledging."))
        if "builder" in st.rotate:
            blocks.append(Block("bridge", f"The builder's next message opens a fresh session ({st.rotate['builder']}); make your REPLY self-contained."))
        blocks.extend(self.supervisor_contract_blocks())
        for item in st.owner_queue:
            if item["to_supervisor"] and not item["shown"]:
                if not item["to_builder"]:
                    note = "(verbatim; binding; for you)"
                elif item["delivered"]:
                    note = "(verbatim; binding; already delivered to the builder)"
                else:
                    note = "(verbatim; binding; delivered to the builder with your REPLY)"
                blocks.append(Block("owner", item["text"], note=note))
        if st.feed:
            told = render([Block(f["origin"], f["text"]) for f in st.feed]).strip()
            blocks.append(Block("bridge", f"Besides your last REPLY, the builder was told:\n{told}"))
        report = Path(review["report"]).read_text(encoding="utf-8") if review.get("report") else ""
        if len(report) > REPORT_CHARS:
            report = f"[bridge] (the first {len(report) - REPORT_CHARS:,} characters are omitted; full text: {review['report']})\n" + report[-REPORT_CHARS:]
        message = render(blocks) + f"===== BUILDER REPORT (exchange {review.get('exchange', st.exchange)}) =====\n{report.strip()}\n===== END =====\n"
        return message, blocks

    def supervisor_contract_blocks(self) -> list[Block]:
        return []

    def _supervisor_rules_text(self) -> str | None:
        path = self.cfg.project.supervisor_rules
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _since_text(self, review: dict[str, Any]) -> str:
        then, now_head = self.st.supervisor_head, self.repo.head()
        if then and now_head and then != now_head:
            commits = self.repo.new_commits(then, now_head)
            subjects = "; ".join(c.subject for c in commits[:8]) + ("; ..." if len(commits) > 8 else "")
            head = f"HEAD {then[:7]}..{now_head[:7]} ({len(commits)} commits: {subjects})"
        else:
            head = f"HEAD unchanged at {now_head[:7]}" if now_head else "no commits yet"
        changed = len(self.repo.status())
        dur = review.get("duration_s")
        turn = f" Builder turn: {int(dur // 60)}m{int(dur % 60):02d}s." if dur is not None else ""
        return f"Since your last turn: {head}; working tree: {changed} uncommitted path(s).{turn}"

    def _supervisor_turn(self) -> int | None:
        st = self.st
        backend = self.backends["supervisor"]
        review = st.review or {}
        if not review.get("counted"):
            st.exchange += 1
            review["exchange"] = st.exchange
            review["counted"] = True
            st.review = review
        rotate_reason = st.rotate.pop("supervisor", None)
        if rotate_reason:
            self.retire_session("supervisor", rotate_reason)
            st.handoff["supervisor"] = rotate_reason
        new_session = backend.session_id is None
        if new_session:
            backend.system_prompt = prompts.supervisor_role(self.cfg, self.enforcement["supervisor"], self._supervisor_rules_text())
        message, _ = self.supervisor_message(new_session=new_session)
        self.save()
        out = self._ask_supervisor(message)
        if out is None or isinstance(out, int):
            return out
        st.handoff.pop("supervisor", None)
        for item in st.owner_queue:
            if item["to_supervisor"]:
                item["shown"] = True
        self._prune_owner_queue()
        return self._apply_supervisor(out)

    def _ask_supervisor(self, message: str) -> SupervisorOutput | int | None:
        """Send, check the tripwire, parse, and nudge once for each malformed shape."""
        n = self.st.exchange
        self.j.transcript(f"EXCHANGE {n} | to supervisor ({self.backends['supervisor'].describe()})", message)
        nudges: list[str] = self.st.review.setdefault("nudges", []) if self.st.review else []
        text = ""
        for attempt in range(4):
            before = self.repo.snapshot()
            try:
                reply = self._send("supervisor", message)
            except BackendError as e:
                return self._turn_failed("supervisor", e)
            after = self.repo.snapshot()
            self._tripwire("supervisor", reply, before, after)
            self._check_models("supervisor", reply)
            text = reply.text
            self.j.transcript(f"EXCHANGE {n} | supervisor reply", text)
            out = parse_supervisor(text, now=self.now())
            if not text.strip():
                nudge = "empty"
            elif not out.has_reply and not out.complete:
                nudge = "no_reply"
            elif out.scope is None and not out.complete:
                nudge = "no_scope"
            else:
                nudge = self.scope_violation(out, nudges)
            if nudge is None:
                return out
            if nudge in nudges:
                return self._malformed(nudge, out)
            nudges.append(nudge)
            self.save()
            message = render([Block("bridge", self._nudge_text(nudge, out))])
            self.j.transcript(f"EXCHANGE {n} | to supervisor (nudge: {nudge})", message)
        return self._malformed("repeated", parse_supervisor(text, now=self.now()))

    def _nudge_text(self, nudge: str, out: SupervisorOutput) -> str:
        return {
            "empty": prompts.EMPTY_NUDGE,
            "no_reply": prompts.NO_REPLY_NUDGE,
            "no_scope": prompts.NO_SCOPE_NUDGE,
        }.get(nudge) or self.scope_nudge_text(out)

    def scope_violation(self, out: SupervisorOutput, nudges: list[str]) -> str | None:
        return None

    def scope_nudge_text(self, out: SupervisorOutput) -> str:
        return prompts.NO_SCOPE_NUDGE

    def _malformed(self, nudge: str, out: SupervisorOutput) -> SupervisorOutput | int | None:
        if nudge == "empty":
            return self._turn_failed("supervisor", _transient("supervisor returned no output twice"))
        if nudge == "no_reply":
            self.j.review(f"SUPERVISOR REPLY WITHOUT REPLY: at exchange {self.st.exchange}", "the whole output was sent to the builder (C5)")
            out.reply = out.raw.strip()
            return out
        if nudge == "no_scope":
            self.j.review(f"SUPERVISOR REPLY WITHOUT SCOPE at exchange {self.st.exchange}", "sent anyway after one nudge")
            return out
        if nudge.startswith("scope"):
            return self.scope_refused(out)
        return self.pause("error", "the supervisor's output stayed malformed after one nudge for each problem")

    def scope_refused(self, out: SupervisorOutput) -> int:
        return EXIT_PAUSED

    def _tripwire(self, role: str, reply: Reply, before: Snapshot, after: Snapshot) -> None:
        if before.fingerprint() != after.fingerprint():
            paths = self.repo.changed_paths(before, after)
            self.j.review(
                f"{role.upper()} TURN CHANGED THE REPO (session {reply.session_id}, exchange {self.st.exchange})",
                "\n".join(paths) + "\n(a background job or the owner may also have written; the tool calls below show what the agent did)",
            )
        writes = [c for c in reply.tool_calls if c.mutating]
        if writes:
            self.j.review(
                f"{role.upper()} USED WRITE TOOLS (session {reply.session_id}, exchange {self.st.exchange})",
                "\n".join(f"{c.name}: {c.summary}" for c in writes),
            )

    def _apply_supervisor(self, out: SupervisorOutput) -> int | None:
        st = self.st
        n = st.exchange
        verdict = out.verdict or "(no verdict)"
        st.last_verdict = {"exchange": n, "text": verdict, "at": iso(self.now())}
        self.j.event("verdict", exchange=n, verdict=verdict, scope=out.scope_text, complete=out.complete)
        self.j.console(f"[{self.now():%H:%M:%S}] exchange {n} VERDICT: {verdict}")
        for warning in out.warnings:
            self.j.review(f"SUPERVISOR OUTPUT at exchange {n}", warning)
        st.feed = []
        st.supervisor_head = self.repo.head()
        if out.complete:
            return self._complete(out)
        if out.escalate and self.cfg.project.mode == "escalate":
            st.pending = new_pending(supervisor=out.reply)
            return self.pause("escalation", out.escalate, resume="BUILDER_TURN")
        handled = self.handle_directives(out)
        if handled is not None:
            return handled
        reply_text = out.reply or ""
        note = ""
        if self.confirm is not None:
            decided = self.confirm(verdict, reply_text)
            if decided is None:
                self.save()
                self.j.console("Discarded by the owner; the supervisor drafts again on the next run.")
                return EXIT_OK
            if decided != reply_text:
                reply_text, note = decided, "(edited by the owner)"
        st.pending = new_pending(supervisor=reply_text, supervisor_note=note)
        st.review = None
        self._replies_this_run += 1
        self.after_reply(out)
        if st.phase != "WAITING":
            st.phase = "BUILDER_TURN"
        self.save()
        return None

    def handle_directives(self, out: SupervisorOutput) -> int | None:
        """REPLAN and friends; extended in later steps. None means: deliver the reply."""
        return None

    def after_reply(self, out: SupervisorOutput) -> None:
        """WAIT, PHASE COMPLETE and rotation; extended in later steps."""

    def _complete(self, out: SupervisorOutput) -> int:
        st = self.st
        summary = out.completion_text()
        st.complete = {"at": iso(self.now()), "exchange": st.exchange, "summary": summary}
        st.phase = "COMPLETE"
        st.review = None
        sup = st.sessions.get("supervisor") or {}
        st.sessions["supervisor"] = {**sup, "closed": True}
        self.save()
        self.j.review(f"PROJECT COMPLETE at exchange {st.exchange}", summary)
        self.j.event("complete", exchange=st.exchange)
        self.j.console(f"PROJECT COMPLETE at exchange {st.exchange}")
        if self.on_complete:
            self.on_complete(self)
        return EXIT_OK

    # ------------------------------------------------------------------ sending and failures

    def _send(self, role: str, message: str) -> Reply:
        backend = self.backends[role]
        raw = self.sd.turns / f"{self.st.exchange:04d}-{role}.jsonl"
        started = self.now()
        self.j.event("turn_start", role=role, exchange=self.st.exchange, engine=backend.describe(), session=backend.session_id)

        def on_event(ev: Event) -> None:
            self.j.event("agent", role=role, type=ev.kind, text=ev.text[:2000], tool=ev.tool)

        reply = backend.send(
            message,
            timeout=backend.cfg.timeout,
            on_event=on_event,
            cancel=_StopNow(self.sd),
            on_session=lambda sid: self._record_session(role, sid),
            raw_path=raw,
        )
        self.j.event(
            "turn_end",
            role=role,
            exchange=self.st.exchange,
            seconds=round((self.now() - started).total_seconds(), 1),
            session=reply.session_id,
            models=sorted(reply.served_models),
            context_tokens=reply.context_tokens,
        )
        self._prune_turn_files()
        return reply

    def _prune_turn_files(self, keep: int = 200) -> None:
        files = sorted(self.sd.turns.glob("*"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep] if len(files) > keep else []:
            p.unlink(missing_ok=True)

    def _turn_failed(self, role: str, e: BackendError) -> int | None:
        """Step-3 handling: unsupported stops; anything else pauses with the error. Extended later."""
        self.j.event("turn_error", role=role, error=type(e).__name__, message=str(e))
        if isinstance(e, Unsupported):
            raise FatalError(f"{role}: {e}")
        if isinstance(e, AuthFailed):
            return self.pause("auth", f"{role}: {e}. Run `claude login` (or re-enroll the pool account), then `agent-bridge run`.")
        return self.pause("error", f"{role}: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ stop and pause

    def unsent_message(self) -> str | None:
        if self.st.pending and (self.st.pending.get("supervisor") or self.st.pending.get("planner") or self.st.pending.get("notes")):
            return self.builder_message()[0]
        if self.st.wait and self.st.wait.get("pending"):
            saved, self.st.pending = self.st.pending, self.st.wait["pending"]
            try:
                return self.builder_message()[0]
            finally:
                self.st.pending = saved
        return None

    def _stop(self) -> int:
        unsent = self.unsent_message()
        if unsent:
            atomic_write_text(self.sd.unsent, unsent)
        self.pause("stop", f"STOP file found; unsent message saved to {self.sd.unsent}" if unsent else "STOP file found", resume=self.st.phase)
        self.j.console(f"Stopped at exchange {self.st.exchange}." + (f" Unsent message: {self.sd.unsent}" if unsent else ""))
        return EXIT_OK

    def pause(self, reason: str, detail: str, *, resume: str | None = None) -> int:
        st = self.st
        resume_phase = resume or st.phase
        if resume_phase == "PAUSED":
            resume_phase = (st.pause or {}).get("resume", "IDLE")
        st.pause = {"reason": reason, "detail": detail, "resume": resume_phase, "at": iso(self.now())}
        st.phase = "PAUSED"
        self.save()
        atomic_write_text(self.sd.paused, self._pause_summary())
        if reason != "stop":
            self.j.review(f"PAUSED ({reason}) at exchange {st.exchange}", detail)
        self.j.event("paused", reason=reason, detail=detail, resume=resume_phase)
        self.j.console(f"PAUSED ({reason}): {detail}")
        return EXIT_OK if reason == "stop" else EXIT_PAUSED

    def _pause_summary(self) -> str:
        st = self.st
        pause = st.pause or {}
        lines = [
            f"# Paused: {pause.get('reason')}",
            "",
            f"- When: {pause.get('at')}",
            f"- Why: {pause.get('detail')}",
            f"- Exchange: {st.exchange}",
            f"- Resumes in: {pause.get('resume')}",
            f"- Last verdict: {(st.last_verdict or {}).get('text', '(none)')}",
            f"- Counters: {st.counters}",
        ]
        if st.review and st.review.get("decisions"):
            lines.append("- Open DECISIONS NEEDED from the builder:")
            lines += [f"  - {d}" for d in st.review["decisions"]]
        if self.sd.unsent.exists():
            lines.append(f"- Unsent message: {self.sd.unsent}")
        lines += ["", "Continue with: `agent-bridge run --forever` (it resumes from the saved state)."]
        return "\n".join(lines) + "\n"


class _StopNow:
    def __init__(self, sd: StateDir) -> None:
        self.sd = sd

    def is_set(self) -> bool:
        return self.sd.stop_now_requested()


def _transient(message: str) -> BackendError:
    from agent_bridge.backends.base import TransientError

    return TransientError(message)
