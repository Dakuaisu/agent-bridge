# Morning report: the overnight build of agent-bridge (2026-10-06)

Everything is committed locally with the repo's Dakuaisu identity, and nothing was pushed.
There is no remote. The three projects (FilingQA, xbrl-frontier, netcode-testbed) were
only read. opencode is still 1.18.30, and no settings were changed.

## 1. What's built and working, by DESIGN.md step

| Step (DESIGN 17) | Commit(s) | State |
|---|---|---|
| Inventory and design v1–v3 | 56f9cbd, 88d4a61, 2b318a4 | done |
| 1. Skeleton, TOML config, statedir, journal | 08c0d23 | done, tested |
| 2. Protocol (three grammars), limits, waits | 0b76d0f | done, tested |
| 3. Backend base, FakeBackend, engine core | ed65367 | done, tested |
| 4. Waits, limits, pauses, budget, rotation, `say`, crash recovery | 1816806 | done, tested with fakes |
| 5. Planner and contract (`new`, interview, `approve`, ledger, re-plan, `decide`, drift) | 07e651d | done, tested; `new`/`approve` also live |
| 6. ClaudeCodeBackend | 5b7ec4c | done, **live-tested** (Haiku) |
| 7. OpencodeBackend and server management | 91cf265 | done, **live-tested once** (Haiku), see L22 |
| 8. CLI, live view, `review`, `report` | 23641c8 | done, tested; `init`/`check`/`status`/`report` live |
| 9. Live smoke tests | `scripts/smoke.sh` in 597dcf2 | Claude Code: 3 runs. opencode: 1 run |
| 10. MIGRATION.md and README | 95f38bb, b5182c2 | written; the migration was **not performed** |

Fixes from the live runs and the final checks:
- 95e4459 and 2f03232: the default effort goes only with the default model.
- 597dcf2: planner fixes from the first smoke run.
- 7e155ec: `sessions.json` records each session's real directory.
- 05fdd5c: the builder stays in its repo (L22).
- 76c3218: the completion hint, and `.omo/` treated as tool state.
- bf64e36: adopted supervisor sessions get the role text once.
- 21fdf63: headings follow CommonMark fences (FilingQA's PRD hid all its phases), and
  sessions resume only on their own engine.

## 2. Test results

**Unit tests, final run (`.venv/bin/python -m pytest -o addopts="" -q`, exit 0):**

```
264 passed in 86.73s (0:01:26)
```

Disclosure: 95e4459 was committed with one failing test, because a pipe hid pytest's
exit code. 2f03232 fixed it the next commit (250 passed).

**Live smoke tests**, all roles on `claude-haiku-4-5-20251001`, in temporary repos under
`/tmp`. Each ran only after `pgrep -fl tools/bridge.py` found nothing.

- **Claude Code, final run (05:12, after every fix)** — the full loop to PROJECT COMPLETE:
  - The planner asked 8 questions. `say` drafted the plan (exit 3 at plan review), and
    `approve --loop 2` ran from there.
  - Quoted: "exchange 1 VERDICT: Phase 1 (R-1, R-2, R-3) is complete. wordcount.py and
    test_wordcount.py are implemented per spec; code is correct and all tests pass."
  - "PHASE COMPLETE: Phase 1 (verified by the supervisor at exchange 1)", "BUILDER
    ROTATED (Phase 1 complete)".
  - "PROJECT COMPLETE at exchange 2", exit=0. The owner-review report was written to
    `.bridge/reports/`.
  - Every turn was "served by claude-haiku-4-5-20251001". The read-only roles reported
    "enforced: --tools Read,Grep,Glob".
  - `review.log`: "COMMIT ATTRIBUTION in da7d656". The Haiku builder added a
    `Co-Authored-By` trailer despite the rules, and the bridge flagged it.
  - The repo's own tests passed afterwards: "3 passed".
- **Claude Code, run 2 (02:34)**: PHASE COMPLETE at exchange 1, then the supervisor
  REPLANned a "Phase 2" the PRD never had ("docs/PRD.md contains only Phase 1 definition
  and exit criteria…"), creating PC-001 awaiting the owner. That led to the completion
  hint (DEC-014), which the final run shows working. It ended "Done: 2 exchange(s)",
  exit=0.
- **Claude Code, run 1 (02:30)**: "PAUSED (plan): the planner's output failed the
  bridge's checks twice", exit=3. It exposed the bugs fixed in 597dcf2 (DEC-010–012).
- **opencode (02:41, all roles on opencode, a bridge-started server on port 4100)**:
  - The supervisor caught a false report: "Builder's report is false. Claims Phase 1 is
    complete … but the repository has no commits".
  - Cause: the builder had worked in `~/src/opencode-claude-bridge` instead (L22, OPEN-002,
    repaired; see 4).
  - Also logged: "SUPERVISOR USED WRITE TOOLS" (`bash` git log/status) and a tripwire
    false positive on `.omo/`, now fixed.
  - The final exit line was not captured.
- **No-model-call checks:** `init` and `check` passed (exit 0) for all-Claude-Code,
  Claude-Code-plus-opencode-builder, and all-opencode. The all-opencode case reports
  "read-only by instruction only" for planner and supervisor.

Cleanup: only the sessions the smoke tests created were deleted, by exact id, after
checking their directory (3 opencode sessions; the Claude Code session files of every
run). The opencode server the bridge started was stopped. The temporary repos are gone.

## 3. AUTONOMOUS DECISIONS (all in docs/DECISIONS.md, "owner to review")

- DEC-001: an opencode role needs an explicit `[opencode] port` (no 4096 default).
- DEC-002: every `[project]` path must resolve inside the repo.
- DEC-003: directive keywords are case-sensitive, after stripping markdown.
- DEC-004: only numbered phases (`Phase 3a`) and ids (`R-4`) count for blocking.
- DEC-005: "weakens" means any changed number or exit-criteria line; such a change always
  waits for you.
- DEC-006: contract drift is blamed on the builder only if it happened during its turn.
- DEC-007: phase completions get the ledger status `VERIFIED (phase boundary)`.
- DEC-008: `decide` supersedes a pending review or wait.
- DEC-009: a re-plan that fails the checks twice leaves the plan unchanged.
- DEC-010: the planner always interviews first.
- DEC-011: the planner's PLAN may omit `bridge.toml`; engines and models stay as you gave
  them.
- DEC-012: `approve` refuses while planning is unfinished, and adopting needs the PRD and
  rules to exist.
- DEC-013: a builder write outside the repo (under your home folder) pauses the run.
- DEC-014: the bridge tells the supervisor when every PRD phase is verified, and `.omo/`
  is tool state.
- DEC-015: the default effort goes only with the default model.
- DEC-016: `init --adopt-legacy`; adopted supervisors get the role text once; a session
  resumes only on its own engine.

## 4. OWNER-BLOCKED, skipped or deferred (docs/OPEN.md)

- **OPEN-002 (owner to confirm):** the opencode smoke builder committed `22af7c8` into
  `~/src/opencode-claude-bridge` on `fix/ec2-audit-hardening`. It was never pushed and has
  been undone:
  - the branch is back at `f6cb717`;
  - your uncommitted `src/index.ts` and `src/index.test.ts` were not touched;
  - the commit is kept on `agent-bridge-smoke-accident-20261006`;
  - the files are in `/tmp/agent-bridge-smoke-accident-backup-20261006/`.

  OPEN-002 has the commands to check the repair and to delete or undo it.
- **OPEN-003 (OWNER-BLOCKED):** a live re-run of the opencode smoke test, to confirm the
  L22 fix. It was skipped because that repo holds your uncommitted work.
- **OPEN-001:** resolved. The `claude` login that had expired before the build was valid
  again for all three Claude Code runs. I never logged in.
- **Not performed:** the migration of the three projects (`docs/MIGRATION.md`).
- **Not live-tested:** the default models (Fable planner/supervisor, Opus builder) in a
  full loop, usage-limit sleeps, WAIT directives, `decide`, `stop --now`,
  `--background`, and `pin`. These are covered by fake-backend tests only.

## 5. Known bugs and limits, and next steps

- **On opencode, read-only is by instruction only.** It is audited, not enforced.
- **Builder actions outside the repo are caught only after the turn** (DEC-013).
- **Commit trailers are flagged, not prevented**, as the final run showed.
- **The completion line can carry no verdict.** When the supervisor writes PROJECT
  COMPLETE on line 1 without a VERDICT, `status` shows "(no verdict)". This is cosmetic.
- **xbrl-frontier and netcode-testbed keep their phases in a table**, not `Phase N`
  headings. The supervisor still works from `phases = "PRD section 9"`, but the bridge's
  phase tracking aids are absent there.
- **None of the three projects' PRDs passes the planner-PRD lint** (no Goals or R-ids).
  `approve` only warns.
- **Next steps:**
  1. Read DEC-001–016.
  2. Settle OPEN-002, then run OPEN-003 with the opencode work committed.
  3. Run one short loop on the default models in a scratch repo.
  4. Migrate netcode-testbed first. It is the smallest, and its Phase 0 is owner-blocked
     anyway. Follow MIGRATION.md, using `approve --no-run`.

## 6. Five-minute quick start

```
cd ~/agent-bridge
.venv/bin/python -m pytest -q                 # about 90 s; expect 264 passed
scripts/smoke.sh claude-code                  # about 3 min of Haiku calls in /tmp
```

The smoke script prints:
- the planner's questions;
- the plan;
- two exchanges ending in PROJECT COMPLETE;
- `status`;
- the sessions it created, plus `repo=` and `state=` paths. Delete those afterwards.

Then try the setup check in a scratch repo. It makes no model calls; this exact sequence
passed tonight:

```
mkdir /tmp/ab-try && cd /tmp/ab-try && git init -q
~/agent-bridge/.venv/bin/agent-bridge init --builder opencode:anthropic/claude-opus-5-5
~/agent-bridge/.venv/bin/agent-bridge check      # exit 0; planner and supervisor "enforced"
rm -rf /tmp/ab-try
```

For a real project, follow `docs/MIGRATION.md` section 2. Steps 1–5 make no model calls,
and step 6 uses `--no-run`.
