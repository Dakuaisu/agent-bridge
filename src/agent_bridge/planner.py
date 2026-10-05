"""The planner role and the contract around the loop (docs/DESIGN.md section 6).

ContractEngine is the engine the CLI runs: the core loop plus the approval gate, re-planning,
owner decisions, the blocked set, SCOPE checks and contract drift.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_bridge import contract, prompts
from agent_bridge.backends.base import BackendError, select_text
from agent_bridge.clock import iso
from agent_bridge.config import ROLES, ConfigError, config_from_dict, load_config
from agent_bridge.engine import EXIT_OK, EXIT_PAUSED, EXIT_UNSUPPORTED, Engine, new_pending
from agent_bridge.protocol import (
    Block,
    PlannerOutput,
    SupervisorOutput,
    display_token,
    parse_planner,
    render,
    render_questions,
    scope_tokens,
)
from agent_bridge.repo import Snapshot
from agent_bridge.statedir import atomic_write_json, atomic_write_text, read_json

PLAN_RETRIES = 1


class PlanError(Exception):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


class ContractEngine(Engine):
    _contract_before: dict[str, str | None] | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def auto_approve(self) -> bool:
        return bool((self.st.contract or {}).get("auto_approve") or (self.st.planning or {}).get("auto_approve"))

    def writable(self) -> list[str]:
        return [contract.rel(self.cfg, p) for p in self.cfg.planning_docs()]

    def rel(self, path: Path | str) -> str:
        return contract.rel(self.cfg, Path(path))

    def changes(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.sd.plan / "changes").glob("PC-*.json")):
            data = read_json(p)
            if isinstance(data, dict):
                out.append(data)
        return out

    def _save_change(self, record: dict[str, Any]) -> None:
        atomic_write_json(self.sd.plan / "changes" / f"{record['id']}.json", record)

    def current_phase(self) -> str | None:
        done = {contract.phase_token(d["phase"]) or d["phase"].lower() for d in self.st.phases_done}
        for h in contract.headings(_read(self.cfg.project.prd)):
            token = contract.phase_token(h.title)
            if token and token not in done:
                return h.title
        return None

    def _rehash_contract(self) -> None:
        if self.st.contract is not None:
            self.st.contract["hashes"] = contract.contract_hashes(self.cfg)

    def blocked_map(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for pc, tokens in self.st.blocked.get("changes", {}).items():
            for token in tokens:
                found.setdefault(token, f"waiting on {pc}")
        for token, reason in self.st.blocked.get("phases", {}).items():
            found[token] = reason
        return found

    # ------------------------------------------------------------------ the loop

    def can_run(self) -> bool:
        planning = self.st.phase in ("PLANNING", "INTERVIEW", "DRAFTING", "PLAN_REVIEW") or (
            self.st.phase == "PAUSED" and (self.st.pause or {}).get("resume") in ("PLANNING", "INTERVIEW")
        )
        return self.st.contract is not None or planning

    def run(self, *, exchanges: int | None = None) -> int:
        if not self.can_run():
            self.j.console(
                "No approved contract. Start a project with `agent-bridge new`, or adopt this repo's PRD and rules "
                "with `agent-bridge approve`."
            )
            return EXIT_UNSUPPORTED
        if self.st.contract is not None:
            changed = contract.drifted(self.cfg, self.st.contract.get("hashes", {}))
            if changed and changed != self.st.contract.get("outside_drift"):
                self.st.contract["outside_drift"] = changed
                self.save()
                self.j.review(
                    "CONTRACT CHANGED OUTSIDE A TURN",
                    f"{', '.join(changed)} differ from the approved versions. If you changed them, approve the current files "
                    "with `agent-bridge approve`.",
                )
                self.j.console(f"warning: {', '.join(changed)} changed since approval; `agent-bridge approve` re-approves them")
        return super().run(exchanges=exchanges)

    def step(self) -> int | None:
        if self.st.phase == "INTERVIEW":
            self.j.console("The planner is waiting for your answers: `agent-bridge say \"<answers>\"`.")
            return EXIT_PAUSED
        if self.st.phase == "PLAN_REVIEW":
            self.j.console("The plan is waiting for your approval: review the files, then `agent-bridge approve`.")
            return EXIT_PAUSED
        return super().step()

    # ------------------------------------------------------------------ talking to the planner

    def _ask_planner(self, blocks: list[Block], heading: str) -> PlannerOutput | int | None:
        backend = self.backends["planner"]
        if backend.session_id is None:
            role = prompts.planner_role(self.cfg, self.enforcement["planner"], self.writable())
            backend.system_prompt = role
            blocks = [Block("bridge", role), *blocks]
        message = render(blocks)
        self.j.transcript(f"PLANNER | to planner ({backend.describe()}): {heading}", message)
        self.j.event("message", recipient="planner", heading=heading, blocks=[{"origin": b.origin, "note": b.note} for b in blocks])
        before = self.repo.snapshot()
        try:
            reply = self._send("planner", message)
        except BackendError as e:
            return self._turn_failed("planner", e)
        after = self.repo.snapshot()
        self._tripwire("planner", reply, before, after)
        self._check_models("planner", reply)
        self.j.transcript("PLANNER | planner reply", reply.text)
        for key in ("timeouts_planner", "questions_planner", "errors_planner", "limit_sleep_s"):
            self.st.counters.pop(key, None)
        out = parse_planner(select_text(reply, ("QUESTIONS:", "PLAN:", "CHANGE:", "NO CHANGE:")))
        self.j.event("planner_output", form=out.kind, errors=out.errors)
        return out

    def _tracked_files(self) -> int:
        return len([line for line in self.repo.out("ls-files").splitlines() if line])

    # ------------------------------------------------------------------ new: interview, drafting

    def plan_new(self, idea: str, *, auto_approve: bool) -> None:
        self.st.planning = {
            "stage": "interview",
            "idea": idea,
            "auto_approve": auto_approve,
            "batches": 0,
            "answers": [],
            "attempts": 0,
        }
        self.st.phase = "PLANNING"
        self.save()
        self.j.event("plan_new", idea=idea, auto_approve=auto_approve)

    def planning_step(self) -> int | None:
        pl = self.st.planning or {}
        stage = pl.get("stage", "interview")
        if pl.get("fix"):
            blocks = [Block("bridge", prompts.planner_fix(pl["fix"]))]
        elif stage == "interview":
            blocks = [
                Block("owner", pl["idea"], note="(verbatim; the project idea)"),
                Block("bridge", prompts.planner_interview(self.cfg, self._tracked_files())),
            ]
        else:
            blocks = []
            if pl.get("auto_answers"):
                blocks.append(Block("bridge", prompts.AUTO_ANSWERS))
            elif pl.get("answers"):
                blocks.append(Block("owner", pl["answers"][-1], note="(verbatim; the owner's answers to your questions)"))
            blocks.append(Block("bridge", prompts.planner_draft(self.cfg, _read(self.cfg.path), may_ask_again=pl.get("batches", 0) < 2)))
        out = self._ask_planner(blocks, heading=stage)
        if not isinstance(out, PlannerOutput):
            return out
        pl.pop("fix", None)
        if out.kind == "questions" and not out.errors and (stage == "interview" or pl.get("batches", 0) < 2):
            return self._interview(out, pl, purpose="new")
        if out.kind == "plan":
            errors, files, warnings = self.validate_plan(out)
            if errors:
                return self._plan_retry(errors)
            self.write_plan(out, files, warnings)
            if pl.get("auto_approve"):
                self.approve_plan(auto=True)
                return None
            self.st.phase = "PLAN_REVIEW"
            self.save()
            self.j.console(self.plan_summary())
            return EXIT_PAUSED
        return self._plan_retry(out.errors or [f"expected {'QUESTIONS or PLAN' if stage == 'interview' else 'PLAN'}, got {out.kind}"])

    def _interview(self, out: PlannerOutput, pl: dict[str, Any], *, purpose: str) -> int | None:
        pl["batches"] = pl.get("batches", 0) + 1
        pl["questions"] = [asdict(q) for q in out.questions]
        pl["interview_for"] = purpose
        text = render_questions(out.questions)
        atomic_write_text(self.sd.plan / f"questions-{pl['batches']}.md", text + "\n")
        self.st.planning = pl
        auto = pl.get("auto_approve") or (purpose == "decide" and (self.st.replan or {}).get("auto_approve"))
        if auto:
            if purpose == "decide":
                self.st.replan["auto_answers"] = True
                self.st.phase = "REPLANNING"
            else:
                pl["auto_answers"] = True
                pl["stage"] = "draft"
            self.save()
            self.j.event("interview_auto", purpose=purpose, questions=len(out.questions))
            return None
        self.st.phase = "INTERVIEW"
        self.save()
        self.j.event("interview", purpose=purpose, questions=len(out.questions))
        self.j.console(f"The planner asks:\n\n{text}\n\nAnswer with `agent-bridge say \"<answers>\"`, or `agent-bridge say \"use your recommendations\"`.")
        return EXIT_PAUSED

    def plan_answer(self, text: str) -> None:
        pl = self.st.planning or {}
        answer = text.strip() or "use your recommendations (the owner sent an empty reply)"
        pl.setdefault("answers", []).append(answer)
        pl.pop("auto_answers", None)
        pl["attempts"] = 0
        if pl.get("interview_for") == "decide" and self.st.replan is not None:
            self.st.replan.setdefault("answers", []).append(answer)
            self.st.phase = "REPLANNING"
        else:
            pl["stage"] = "draft"
            self.st.phase = "PLANNING"
        self.st.planning = pl
        self.save()
        self.j.event("owner_answers", text=answer)

    def _plan_retry(self, errors: list[str]) -> int | None:
        pl = self.st.planning or {}
        pl["attempts"] = pl.get("attempts", 0) + 1
        atomic_write_text(self.sd.plan / "errors.md", "\n".join(f"- {e}" for e in errors) + "\n")
        if pl["attempts"] > PLAN_RETRIES:
            return self.pause("plan", "the planner's output failed the bridge's checks twice:\n" + "\n".join(errors), resume="PLANNING")
        pl["fix"] = errors
        self.st.planning = pl
        self.save()
        self.j.event("plan_rejected", errors=errors)
        return None

    def validate_plan(self, out: PlannerOutput) -> tuple[list[str], dict[Path, str], list[str]]:
        cfg = self.cfg
        errors, warnings = list(out.errors), []
        wanted = {p.resolve(): self.rel(p) for p in cfg.planning_docs()}
        files: dict[Path, str] = {}
        for f in out.files:
            path = (cfg.project.repo / f.path).resolve()
            if path not in wanted:
                errors.append(f"{f.path}: not one of the contract files ({', '.join(wanted.values())})")
                continue
            files[path] = f.content
        for path, name in wanted.items():
            if path not in files:
                errors.append(f"missing file {name}")
        prd = cfg.project.prd.resolve()
        if prd in files:
            errors += contract.lint_prd(files[prd])
        rules = cfg.project.rules[0].resolve()
        if rules in files and not files[rules].strip():
            errors.append(f"{self.rel(rules)} is empty")
        decisions = cfg.project.decisions.resolve()
        if decisions in files and "## DEC-" not in files[decisions]:
            errors.append(f"{self.rel(decisions)} has no '## DEC-' entries")
        if not out.kickoff.strip():
            errors.append("no KICKOFF")
        if cfg.path.resolve() in files:
            errors += self._check_planner_config(files[cfg.path.resolve()], warnings)
        return errors, files, warnings

    def _check_planner_config(self, text: str, warnings: list[str]) -> list[str]:
        try:
            new = config_from_dict(tomllib.loads(text), self.cfg.path)
        except tomllib.TOMLDecodeError as e:
            return [f"bridge.toml: not valid TOML: {e}"]
        except ConfigError as e:
            return [f"bridge.toml: {p}" for p in e.problems]
        errors = []
        old = self.cfg
        if new.project.repo != old.project.repo:
            errors.append('bridge.toml: project.repo must stay "."')
        for key in ("prd", "decisions", "open_items"):
            if getattr(new.project, key) != getattr(old.project, key):
                errors.append(f"bridge.toml: keep project.{key} as {self.rel(getattr(old.project, key))}")
        if new.project.rules[0] != old.project.rules[0]:
            errors.append(f"bridge.toml: keep project.rules starting with {self.rel(old.project.rules[0])}")
        for role in ROLES:
            a, b = old.role(role), new.role(role)
            if (a.engine, a.model) != (b.engine, b.model):
                errors.append(f"bridge.toml: keep [{role}] engine and model as given ({a.engine}, {a.model})")
        owner_only = new.owner_only_settings()
        if owner_only:
            if (self.st.planning or {}).get("auto_approve"):
                errors.append(f"bridge.toml: {', '.join(owner_only)} is the owner's to set, never the planner's under --auto-approve")
            else:
                warnings.append(f"bridge.toml sets {', '.join(owner_only)}: an owner-only setting; check it before approving")
        return errors

    def write_plan(self, out: PlannerOutput, files: dict[Path, str], warnings: list[str]) -> None:
        cfg = self.cfg
        pl = self.st.planning or {}
        status = contract.AUTONOMOUS if pl.get("auto_approve") else "PROPOSED"
        for path, content in files.items():
            if path == cfg.project.decisions.resolve():
                content = contract.normalize_seeded_ledger(content, status)
            atomic_write_text(path, content)
        self.cfg = load_config(cfg.path)
        rules = self.cfg.project.rules[0]
        atomic_write_text(rules, contract.apply_block(_read(rules), self.cfg))
        link = contract.ensure_agents_symlink(self.cfg.project.repo, rules)
        if link not in ("ok", "created"):
            warnings.append(link)
        contract.ensure_gitignore(self.cfg.project.repo)
        pl.update(stage="review", summary=out.summary, kickoff=out.kickoff, warnings=warnings, files=[self.rel(p) for p in files])
        self.st.planning = pl
        atomic_write_text(self.sd.plan / "plan.md", self.plan_summary())
        self.save()
        self.j.event("plan_written", files=pl["files"], warnings=warnings)

    def plan_summary(self) -> str:
        pl = self.st.planning or {}
        lines = ["# Plan drafted by the planner", "", pl.get("summary", "").strip(), "", "Files written (uncommitted):"]
        lines += [f"- {f}" for f in pl.get("files", [])]
        if pl.get("warnings"):
            lines += ["", "Check before approving:"] + [f"- {w}" for w in pl["warnings"]]
        lines += ["", "The builder's first instruction:", "", pl.get("kickoff", "").strip(), ""]
        lines.append("Edit the files if you like, then approve with `agent-bridge approve`.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ approval

    def approve_plan(self, *, auto: bool = False, adopt: bool = False) -> list[str]:
        self.cfg = load_config(self.cfg.path)
        cfg = self.cfg
        when = iso(self.now())
        notes: list[str] = []
        decisions = cfg.project.decisions
        if adopt:
            notes += [f"warning: {p}" for p in contract.lint_prd(_read(cfg.project.prd))]
        elif not auto:
            text = _read(decisions)
            for dec in contract.seeded_decisions(text):
                block = re.search(rf"^##\s+{dec}\b.*?(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
                if block and re.search(r"^-\s*Status:\s*PROPOSED\s*$", block.group(0), re.MULTILINE):
                    contract.set_decision_status(decisions, dec, f"APPROVED by the owner {when[:10]}")
        hashes = contract.contract_hashes(cfg)
        listing = "\n".join(f"- {p}: sha256 {h}" for p, h in hashes.items())
        if adopt:
            title, by, status = "Existing contract adopted by the owner", "owner", "OWNER DECISION (agent-bridge approve)"
        elif auto:
            title, by, status = "Plan approved under --auto-approve", "planner (the owner chose --auto-approve)", contract.AUTONOMOUS
        else:
            title, by, status = "Plan approved by the owner", "owner", "OWNER DECISION (agent-bridge approve)"
        if decisions.exists() or not adopt:
            dec, _ = contract.append_decision(decisions, title=title, decided_by=by, status=status, body=f"Recorded by agent-bridge at {when}.\n\n{listing}")
            notes.append(f"recorded {dec} in {self.rel(decisions)}")
        self.st.contract = {"approved_at": when, "by": by, "hashes": hashes, "auto_approve": auto, "drift": [], "outside_drift": []}
        if not adopt:
            pl = self.st.planning or {}
            pl["stage"] = "approved"
            self.st.planning = pl
            self.kickoff(pl.get("kickoff", "").strip(), origin="planner")
        self.save()
        self.j.review(f"CONTRACT APPROVED ({by})", listing)
        self.j.event("contract_approved", by=by, hashes=hashes)
        return notes

    def reapprove_contract(self) -> list[str]:
        changed = contract.drifted(self.cfg, (self.st.contract or {}).get("hashes", {}))
        self.cfg = load_config(self.cfg.path)
        hashes = contract.contract_hashes(self.cfg)
        listing = "\n".join(f"- {p}: sha256 {h}" for p, h in hashes.items())
        dec, _ = contract.append_decision(
            self.cfg.project.decisions,
            title="Contract re-approved by the owner",
            decided_by="owner",
            status="OWNER DECISION (agent-bridge approve)",
            body=f"Changed since the last approval: {', '.join(changed) or 'nothing'}.\n\n{listing}",
        )
        self.st.contract = {**(self.st.contract or {}), "hashes": hashes, "drift": [], "outside_drift": [], "approved_at": iso(self.now())}
        self.save()
        self.j.review("CONTRACT RE-APPROVED (owner)", listing)
        return [f"re-approved {', '.join(changed) or 'the unchanged contract'}; recorded {dec}"]

    def waiting_changes(self) -> list[dict[str, Any]]:
        return [c for c in self.changes() if c.get("status") == "awaiting"]

    def approve_changes(self, ids: list[str], *, reject: bool = False, reason: str = "") -> list[str]:
        waiting = {c["id"]: c for c in self.waiting_changes()}
        chosen = ids or (list(waiting) if not reject else [])
        messages = []
        for pc in chosen:
            record = waiting.get(pc)
            if record is None:
                messages.append(f"{pc}: not a plan change waiting for the owner")
                continue
            when = iso(self.now())
            if reject:
                record.update(status="rejected", settled=when, reason_rejected=reason)
                contract.set_decision_status(self.cfg.project.decisions, record["dec"], f"REJECTED by the owner: {reason or 'no reason given'}")
                self.st.supervisor_notes.append(f'The owner rejected {pc} ({record["title"]}): "{reason}". The plan is unchanged.')
                messages.append(f"{pc}: rejected")
            else:
                from agent_bridge.protocol import Edit

                try:
                    results = contract.apply_edits_in_memory(self.cfg, [Edit(**e) for e in record["edits"]], self.cfg.planning_docs())
                except contract.EditError as e:
                    messages.append(f"{pc}: cannot apply any more ({e}); reject it, or ask for a new re-plan")
                    continue
                contract.write_results(results)
                self._rehash_contract()
                record.update(status="approved", settled=when)
                contract.set_decision_status(self.cfg.project.decisions, record["dec"], f"APPROVED by the owner {when[:10]}")
                self.st.planner_queue.append(f"The owner approved plan change {pc} {record['title']} ({record['dec']}; diff {record['diff']}). Re-read the changed parts.")
                self.st.supervisor_notes.append(f"The owner approved {pc} ({record['title']}); it is now part of the contract.")
                messages.append(f"{pc}: approved and applied")
            if record.get("open_item"):
                contract.set_open_status(self.cfg.project.open_items, record["open_item"], f"RESOLVED ({record['status']} by the owner {when[:10]})")
            self.st.blocked.get("changes", {}).pop(pc, None)
            self._save_change(record)
            self.j.review(f"PLAN CHANGE {pc} {record['status'].upper()} by the owner", reason)
        self.save()
        return messages

    # ------------------------------------------------------------------ owner decisions

    def decide(self, doc: Path, *, auto_approve: bool = False) -> None:
        text = _read(doc)
        problems = []
        if not re.search(r"(?m)^#{1,6}\s+D\d+\b", text):
            problems.append("no D-items (headings like '## D1 - ...')")
        if not re.search(r"(?im)^#{1,6}\s+done when", text) or not re.search(r"(?m)^\s*-\s*\[[ xX]\]", text):
            problems.append('no "Done when" checklist (a "## Done when" heading with "- [ ]" items)')
        if problems:
            raise PlanError(f"{doc}: " + "; ".join(problems))
        if self.st.phase == "WAITING" and self.st.wait:
            self.j.event("wait_dropped", reason="owner decisions arrived")
            self.st.wait = None
        if self.st.review:
            self.j.event("review_skipped", reason="owner decisions arrived", exchange=self.st.exchange)
        self.st.replan = {"source": "owner", "doc": self.rel(doc.resolve()), "attempt": 0, "auto_approve": auto_approve, "answers": []}
        self.st.phase = "REPLANNING"
        self.save()
        self.j.event("decide", doc=self.rel(doc.resolve()), auto_approve=auto_approve)

    def handle_inbox_item(self, kind: str, data: dict[str, Any]) -> bool:
        if kind == "approve":
            for line in self.approve_changes(list(data.get("ids", [])), reject=bool(data.get("reject")), reason=str(data.get("reason", ""))):
                self.j.console(line)
            return False
        if kind == "decide":
            try:
                self.decide(Path(data["doc"]), auto_approve=bool(data.get("auto_approve")))
            except PlanError as e:
                self.j.review("DECIDE REFUSED", str(e))
            return False
        return super().handle_inbox_item(kind, data)

    # ------------------------------------------------------------------ re-planning

    def handle_directives(self, out: SupervisorOutput) -> int | str | None:
        if out.replan is None:
            return None
        st = self.st
        label = out.replan.phase or self.current_phase() or "the current phase"
        token = contract.phase_token(label) or label.strip().lower()
        if token in st.blocked.get("phases", {}):
            st.supervisor_notes.append(
                f"{display_token(token)} is paused ({st.blocked['phases'][token]}). Do not REPLAN it again: direct other "
                "unblocked work, use WAIT, or end at the owner-blocked boundary."
            )
            self.j.event("replan_refused", phase=label, reason=st.blocked["phases"][token])
            return "again"
        count = st.replans.get(token, 0) + 1
        history = st.replan_history.setdefault(token, [])
        history.append(out.replan.problem)
        cap = self.cfg.budget.max_replans_per_phase
        if count > cap:
            item, _ = contract.append_open_item(
                self.cfg.project.open_items,
                title=f"{display_token(token)}: re-plan cap reached",
                status="OWNER-BLOCKED",
                body=(
                    f"The supervisor asked to re-plan {display_token(token)} {count} times (cap {cap}). The phase is paused: "
                    "no work is directed into it until the owner decides (`agent-bridge decide <doc>`) or settles its "
                    "waiting plan changes.\n\nRe-plan requests, oldest first:\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(history, 1))
                ),
            )
            st.blocked.setdefault("phases", {})[token] = f"paused at the re-plan cap, {item}"
            st.supervisor_notes.append(
                f"{display_token(token)} reached the re-plan cap ({cap}); the bridge paused it and recorded {item} as OWNER-BLOCKED. "
                "Direct other unblocked work, use WAIT, or end at the owner-blocked boundary."
            )
            self.j.review(f"RE-PLAN CAP REACHED for {display_token(token)}: phase paused, {item}", out.replan.problem)
            return "again"
        st.replans[token] = count
        st.replan = {
            "source": "supervisor",
            "phase": label,
            "token": token,
            "problem": out.replan.problem,
            "verdict": out.verdict,
            "report": (st.review or {}).get("report"),
            "exchange": st.exchange,
            "attempt": 0,
        }
        st.phase = "REPLANNING"
        self.j.event("replan", phase=label, problem=out.replan.problem, count=count)
        self.j.console(f"[{self.now():%H:%M:%S}] REPLAN ({label}): {out.replan.problem}")
        return None

    def _planner_context(self, token: str | None = None) -> str:
        st = self.st
        hashes = (st.contract or {}).get("hashes", {})
        lines = [
            f"Contract approved {(st.contract or {}).get('approved_at', '(not yet)')}: "
            + ", ".join(f"{p} (sha256 {str(h)[:12]})" for p, h in hashes.items())
        ]
        done = ", ".join(d["phase"] for d in st.phases_done) or "none"
        lines.append(f"Phases verified complete: {done}. Current phase: {self.current_phase() or 'unknown'}.")
        waiting = self.waiting_changes()
        if waiting:
            lines.append("Plan changes waiting for the owner: " + "; ".join(f"{c['id']} {c['title']}" for c in waiting))
        rejected = [c for c in self.changes() if c.get("status") == "rejected"]
        if rejected:
            lines.append("Rejected by the owner: " + "; ".join(f"{c['id']} {c['title']} ({c.get('reason_rejected', '')})" for c in rejected))
        if st.blocked.get("phases"):
            lines.append("Paused phases: " + "; ".join(f"{display_token(t)} ({r})" for t, r in st.blocked["phases"].items()))
        if token:
            lines.append(f"Re-plans for {display_token(token)} so far: {st.replans.get(token, 0)} of {self.cfg.budget.max_replans_per_phase}.")
        return "\n".join(lines)

    def replanning(self) -> int | None:
        st = self.st
        rp = st.replan or {}
        source = rp.get("source")
        if rp.get("fix"):
            blocks = [Block("bridge", prompts.planner_fix(rp["fix"]))]
        elif source == "owner":
            doc = self.cfg.project.repo / rp["doc"]
            blocks = [
                Block("bridge", f"The owner ran `agent-bridge decide {rp['doc']}`. Its contents follow, verbatim."),
                Block("owner", _read(doc), note=f"(verbatim; binding; the contents of {rp['doc']})"),
            ]
            if rp.get("answers"):
                blocks.append(Block("owner", rp["answers"][-1], note="(verbatim; the owner's answers to your questions)"))
            elif rp.get("auto_answers"):
                blocks.append(Block("bridge", prompts.AUTO_ANSWERS))
            blocks += [Block("bridge", self._planner_context()), Block("bridge", prompts.planner_decide(self.cfg, rp["doc"], self.writable()))]
        else:
            report = _read(Path(rp["report"]))[-8000:] if rp.get("report") else ""
            blocks = [
                Block("supervisor", f"REPLAN ({rp['phase']}): {rp['problem']}\n\nMy VERDICT on the builder's last turn: {rp.get('verdict') or '(none)'}"),
                Block("bridge", f"The builder report the supervisor was reviewing (exchange {rp.get('exchange')}), last 8,000 characters:\n{report}"),
                Block("bridge", self._planner_context(rp.get("token"))),
                Block("bridge", prompts.planner_replan(self.cfg, self.writable())),
            ]
        out = self._ask_planner(blocks, heading=f"re-plan ({source})")
        if not isinstance(out, PlannerOutput):
            return out
        rp.pop("fix", None)
        if out.kind == "questions" and not out.errors:
            if source == "owner":
                return self._interview(out, dict(st.planning or {}), purpose="decide")
            item, _ = contract.append_open_item(
                self.cfg.project.open_items,
                title=f"The planner needs the owner to re-plan {rp.get('phase')}",
                status="OWNER-BLOCKED",
                body=f"Re-plan request: {rp.get('problem')}\n\nThe planner's questions:\n{render_questions(out.questions)}",
            )
            return self._answer_supervisor(
                f"The planner cannot re-plan {rp.get('phase')} without the owner. Its questions are recorded as {item} "
                f"(OWNER-BLOCKED):\n{render_questions(out.questions)}"
            )
        if out.kind == "no_change" and not out.errors:
            text = f"NO CHANGE: {out.reason}" + (f"\n\n{out.to_supervisor}" if out.to_supervisor else "")
            return self._answer_supervisor(text)
        if out.kind != "change" or out.errors:
            return self._replan_retry(out.errors or [f"expected CHANGE or NO CHANGE, got {out.kind}"])
        if source == "owner" and not out.kickoff.strip():
            return self._replan_retry(["a CHANGE for owner decisions needs a KICKOFF for the builder"])
        try:
            results = contract.apply_edits_in_memory(self.cfg, out.edits, self.cfg.planning_docs())
        except contract.EditError as e:
            return self._replan_retry([str(e)])
        return self._record_change(out, results)

    def _replan_retry(self, errors: list[str]) -> int | None:
        rp = self.st.replan or {}
        rp["attempt"] = rp.get("attempt", 0) + 1
        if rp["attempt"] > PLAN_RETRIES:
            if rp.get("source") == "owner":
                return self.pause("plan", "the planner's answer to the owner's decisions failed the bridge's checks twice:\n" + "\n".join(errors), resume="REPLANNING")
            self.j.review("RE-PLAN FAILED THE CHECKS TWICE", "\n".join(errors))
            return self._answer_supervisor(
                "The planner's answer failed the bridge's checks twice (" + "; ".join(errors) + "). The plan is unchanged; "
                "continue under the current contract."
            )
        rp["fix"] = errors
        self.st.replan = rp
        self.save()
        self.j.event("replan_rejected", errors=errors)
        return None

    def _answer_supervisor(self, text: str) -> None:
        st = self.st
        review = st.review or {}
        review.setdefault("planner", []).append(text)
        st.review = review
        st.replan = None
        st.phase = "SUPERVISOR_TURN"
        self.save()
        return None

    def _record_change(self, out: PlannerOutput, results: list[contract.EditResult]) -> int | None:
        st, cfg = self.st, self.cfg
        rp = st.replan or {}
        source = rp.get("source")
        pc = f"PC-{len(list((self.sd.plan / 'changes').glob('PC-*.json'))) + 1:03d}"
        diff_path = self.sd.plan / "changes" / f"{pc}.diff"
        atomic_write_text(diff_path, contract.unified_diff(cfg, results))
        diff_rel = f".bridge/plan/changes/{pc}.diff"
        assessment = contract.assess(cfg, out.edits, out.material)
        affects = sorted(out.affects | assessment.affects)
        if source == "owner":
            outcome = "owner"
        elif assessment.weakens:
            outcome = "awaiting"
        elif not assessment.material:
            outcome = "applied"
        elif self.auto_approve:
            outcome = "autonomous"
        else:
            outcome = "awaiting"
        status = {
            "owner": f"OWNER DECISION ({rp.get('doc')})",
            "applied": "APPLIED (non-material)",
            "autonomous": contract.AUTONOMOUS,
            "awaiting": "AWAITING OWNER",
        }[outcome]
        decided_by = (
            f"planner (the owner's decisions in {rp.get('doc')})"
            if source == "owner"
            else f"planner (re-plan requested by the supervisor at exchange {rp.get('exchange')})"
        )
        why_wait = f"\n\nWhy it waits for the owner: {'; '.join(assessment.reasons)}" if outcome == "awaiting" and assessment.reasons else ""
        body = f"Reason: {out.reason}\n\n{out.ledger}".strip() + why_wait
        dec, line = contract.append_decision(cfg.project.decisions, title=f"{pc} {out.title}", decided_by=decided_by, status=status, change=f"{pc} ({diff_rel})", body=body)
        record: dict[str, Any] = {
            "id": pc,
            "title": out.title,
            "reason": out.reason,
            "material": assessment.material,
            "planner_material": out.material,
            "weakens": assessment.weakens,
            "reasons": assessment.reasons,
            "affects": affects,
            "edits": [asdict(e) for e in out.edits],
            "diff": diff_rel,
            "status": outcome,
            "dec": dec,
            "dec_line": line,
            "source": source,
            "exchange": st.exchange,
            "created": iso(self.now()),
            "open_item": None,
        }
        shown = ", ".join(display_token(t) for t in affects) or "see the diff"
        if outcome == "awaiting":
            item, _ = contract.append_open_item(
                cfg.project.open_items,
                title=f"{pc} {out.title} waits for the owner",
                status="OWNER-BLOCKED",
                body=(
                    f"The planner proposed a plan change that needs the owner's approval: "
                    f"{'it weakens an exit criterion or a threshold' if assessment.weakens else 'it is material'}.\n\n"
                    f"Reason: {out.reason}\nAffects: {shown}\nDiff: {diff_rel}\nLedger: {dec}\n\n"
                    f"Approve with `agent-bridge approve {pc}`, or reject with `agent-bridge approve --reject {pc} --reason \"...\"`. "
                    "Until then the affected requirements and phases are blocked."
                ),
            )
            record["open_item"] = item
            st.blocked.setdefault("changes", {})[pc] = affects
        else:
            contract.write_results(results)
            self._rehash_contract()
            record["applied_at"] = iso(self.now())
            if source != "owner":
                st.planner_queue.append(f"The plan changed: {pc} {out.title} ({dec}; diff {diff_rel}). Re-read the changed parts: {shown}.")
        self._save_change(record)
        self.j.review(f"PLAN CHANGE {pc} ({outcome}): {out.title}", f"{status}; affects {shown}; {dec}; diff {diff_rel}")
        self.j.event("plan_change", id=pc, outcome=outcome, affects=affects, dec=dec)
        if source == "owner":
            st.replans, st.replan_history = {}, {}
            st.blocked["phases"] = {}
            st.rotate["supervisor"] = "the owner's decisions were applied"
            st.complete = None
            st.replan = None
            st.review = None
            st.pending = new_pending(planner=[f"{out.kickoff.strip()}\n\n(The plan change is {pc}, recorded as {dec}; diff {diff_rel}.)"])
            st.phase = "BUILDER_TURN"
            self.save()
            return None
        if outcome == "awaiting":
            text = (
                f"{pc} {out.title}: NOT applied; it waits for the owner "
                f"({'it weakens an exit criterion or a threshold' if assessment.weakens else 'material change'}). "
                f"Recorded as {dec} and {record['open_item']}. Until the owner settles it, {shown} are blocked."
            )
        else:
            text = f"{pc} {out.title}: applied ({status}); recorded as {dec}; diff {diff_rel}. Affects: {shown}."
        if out.to_supervisor:
            text += f"\n\n{out.to_supervisor}"
        return self._answer_supervisor(text)

    # ------------------------------------------------------------------ hooks into the core loop

    def blocked_tokens(self) -> list[str]:
        return [f"{display_token(t)} ({reason})" for t, reason in sorted(self.blocked_map().items())]

    def supervisor_contract_blocks(self) -> list[Block]:
        blocked = self.blocked_tokens()
        if not blocked:
            return []
        return [Block("bridge", f"Blocked: {', '.join(blocked)}. Your SCOPE must avoid them.")]

    def scope_violation(self, out: SupervisorOutput) -> str | None:
        blocked = self.blocked_map()
        if not blocked or out.complete:
            return None
        if out.scope and (hit := out.scope & set(blocked)):
            self._scope_hit = sorted(hit)
            return "scope"
        mentioned = scope_tokens(out.reply or "") & set(blocked)
        if mentioned:
            self.j.review(
                f"SUPERVISOR REPLY MENTIONS BLOCKED ITEMS at exchange {self.st.exchange}",
                f"{', '.join(display_token(t) for t in sorted(mentioned))} (sent: a mention can be a warning to stay away)",
            )
        return None

    def scope_nudge_text(self, out: SupervisorOutput) -> str:
        blocked = self.blocked_map()
        hit = ", ".join(f"{display_token(t)} ({blocked[t]})" for t in getattr(self, "_scope_hit", []))
        return (
            f"Your SCOPE ({out.scope_text}) includes blocked items: {hit}. They wait for the owner. Direct work outside "
            "them, use WAIT, or write SCOPE: none. Reply again in the required shape."
        )

    def scope_refused(self, out: SupervisorOutput) -> int:
        held = self.sd.plan / f"held-reply-{self.st.exchange:04d}.md"
        atomic_write_text(held, out.raw)
        return self.pause(
            "scope",
            f"the supervisor directed work into blocked items twice ({', '.join(display_token(t) for t in getattr(self, '_scope_hit', []))}); "
            f"its reply was held, not sent: {held}",
            resume="SUPERVISOR_TURN",
        )

    def before_builder_turn(self) -> None:
        self._contract_before = contract.contract_hashes(self.cfg) if self.st.contract else None

    def after_builder_audit(self, before: Snapshot, after: Snapshot) -> list[str]:
        if not self.st.contract or self._contract_before is None:
            return []
        now = contract.contract_hashes(self.cfg)
        changed = sorted(p for p, h in now.items() if self._contract_before.get(p) != h)
        if not changed:
            return []
        diff = self.repo.out("diff", "--", *changed)[:3000]
        self.st.contract["drift"] = sorted(set(self.st.contract.get("drift", [])) | set(changed))
        self.j.review(f"CONTRACT CHANGED BY THE BUILDER at exchange {self.st.exchange}", f"{', '.join(changed)}\n{diff}")
        return [
            f"The builder changed {', '.join(changed)} during its turn. The contract changes only through the planner; have the "
            f"builder restore the approved version (git checkout -- <file>) unless the owner decided otherwise.\n{diff}"
        ]

    def on_phase_complete(self, phase: str) -> None:
        if self.st.contract is None:
            return
        dec, _ = contract.append_decision(
            self.cfg.project.decisions,
            title=f"{phase} complete",
            decided_by=f"supervisor (verified at exchange {self.st.exchange})",
            status="VERIFIED (phase boundary)",
            body="Recorded by agent-bridge from the supervisor's PHASE COMPLETE directive: the supervisor verified this phase's exit criteria in the repo.",
        )
        self.st.phases_done[-1]["dec"] = dec

    def status_lines(self) -> list[str]:
        """Contract facts for `status`."""
        lines = []
        c = self.st.contract
        if c:
            lines.append(f"contract:    approved {c.get('approved_at')} by {c.get('by')}" + (" (auto-approve)" if c.get("auto_approve") else ""))
            drift = contract.drifted(self.cfg, c.get("hashes", {}))
            if drift:
                lines.append(f"             changed since approval: {', '.join(drift)}")
        else:
            lines.append("contract:    not approved yet")
        for change in self.waiting_changes():
            lines.append(f"waiting:     {change['id']} {change['title']} (affects {', '.join(display_token(t) for t in change['affects']) or '?'})")
        for token, reason in self.st.blocked.get("phases", {}).items():
            lines.append(f"paused:      {display_token(token)} ({reason})")
        return lines


def load_questions(sd_plan: Path) -> str:
    files = sorted(sd_plan.glob("questions-*.md"))
    return files[-1].read_text(encoding="utf-8") if files else ""


def to_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
