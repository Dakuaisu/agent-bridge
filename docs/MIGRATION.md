# Moving FillingQA, xbrl-frontier and netcode-testbed onto agent-bridge

Status: FillingQA moved on 2026-10-06 (section 6 says what happened); xbrl-frontier and
netcode-testbed have not moved. Follow this per project, with that project's old bridge
stopped.

## 0. Before you start

- **The old bridge must not be running.** `pgrep -fl tools/bridge.py` should print
  nothing. The old bridge takes no lock, so agent-bridge cannot tell that it is running.
  Never run both on one project.
- **opencode is still 1.18.30** (`opencode --version`), with `"autoupdate": false`.
- **agent-bridge is installed** (README, "Install"), and `agent-bridge --version` works.

## 1. What maps to what

| Old `.bridge/` file | Under agent-bridge |
|---|---|
| `session` | the builder session in `.bridge/state.json` (`init --adopt-legacy`, or `pin --builder ID`) |
| `supervisor_session` | the supervisor session. agent-bridge sends it the new role text once (labels, SCOPE, WAIT, REPLAN), because the protocol changed |
| `builder_last.md` | same name, same meaning. `--adopt-legacy` makes it the next report the supervisor reviews |
| `unsent_reply.md` | `--adopt-legacy` makes it the next builder message, labelled `[supervisor] (saved by the old bridge when it stopped)`, unless `builder_last.md` is newer: then the old bridge ran past it and it is skipped. New unsent messages use the same file |
| `loop.log`, `review.log`, `console.log`, `serve.log` | kept and appended to. `loop.log` now records every message exactly as delivered, labels included; `review.log` keeps the `=== … ===` format |
| `STOP` | same meaning; written by `agent-bridge stop` and cleared by the next run |
| `kickoff*.md`, `resume.md`, `continue_kickoff.md`, `max_generator.md` | not read; keep them for history. Reuse one with `run --kickoff @.bridge/kickoff.md` |
| `relaunch_after_eval.sh` | replaced by `WAIT FOR PID <n>` |
| `_filingqa_copy/` (xbrl-frontier) | not used; delete it when you like |

| Old environment variable | `bridge.toml` |
|---|---|
| `BRIDGE_ATTACH` (port) | `[opencode] port` (4096 FillingQA, 4097 xbrl-frontier, 4098 netcode-testbed) |
| `BRIDGE_SUPERVISOR`, `BRIDGE_MODEL`, `BRIDGE_VARIANT`, `BRIDGE_EFFORT` | `[supervisor] engine`, `model`, `variant` |
| `BRIDGE_BUILDER_MODEL`, `BRIDGE_BUILDER_TIMEOUT` | `[builder] model`, `timeout` |
| `BRIDGE_BUILDER_SKIP_PERMS` | always on (`--auto` for opencode, `bypassPermissions` for Claude Code) |
| `BRIDGE_THINK` ("ultrathink") | dropped; use `variant` (Claude Code's `--effort`) |
| `BRIDGE_IDLE_WAIT` | built in: idle note after 3 unchanged exchanges, backstop sleep after 5, pause at `budget.max_unchanged_exchanges` |
| `<PROJECT>_REPO` | the repo the command runs in, or `--repo` |
| xbrl-frontier's `.env` loading | `[project] env_file = ".env"` |

What the old `SYSTEM` and `AUTONOMOUS` texts become:
- **The generic parts** are built into agent-bridge: metric-low is a finding, no
  fabrication, data wins, phase order, verify claims, autonomous decisions, OWNER-BLOCKED,
  the completion rule.
- **The project parts** go into `docs/SUPERVISOR.md` (`[project] supervisor_rules`),
  which is inlined into the supervisor's first message. Copy them verbatim:

| Project | `SYSTEM` (project parts) | `AUTONOMOUS` (project parts) |
|---|---|---|
| FillingQA | the builder description (lines 131–132) and the docs to read (138–141) | owner-only work (180–185) and the completion check (191–197) |
| xbrl-frontier | the description and docs (154–170), principles 1–2 (175–186) and the section 2 scope rule (189–190) | dependencies (218–221), owner-only work (222–232), Kaggle and money, T-033 (233–242), the completion check (250–253) |
| netcode-testbed | the description and docs (168–185) and principles 1–4 and 6 (190–222) | dependencies (252–257), owner-only work (258–276), quiet host and fallbacks (277–282), the completion check (294–299) |

## 2. Steps for every project

The terminal UI does the same with **Set up this repo** (run `agent-bridge` in the repo):
init, then check, then `a` to adopt. Its old-state toggle is off by default, and it warns
until `docs/SUPERVISOR.md` exists. Steps 1, 3 and 7 below still need you.

1. Create `docs/SUPERVISOR.md` from the line ranges in section 1. Do this first: `init`
   then sets `supervisor_rules` itself, and `check` and every run warn while a repo with
   an old `tools/bridge.py` has none.

2. Write the config. The flags below keep today's engines; the planner is new and runs
   on Claude Code.

   ```
   cd ~/<project>
   agent-bridge init --builder opencode:anthropic/claude-opus-5-5 \
     --supervisor opencode:anthropic/claude-fable-5-1 --port <4096|4097|4098> --write-rules
   ```

   - `init` finds `docs/TRADEOFFS.md` and makes it the ledger, finds `docs/WORKLOG.md`,
     and adds `.bridge/` and `.omo/` to `.gitignore` if missing.
   - `--write-rules` appends the agent-bridge block to `CLAUDE.md` (the labels, WAIT
     lines, the contract, the commit rules) and prints the diff. It writes at once, so
     read the diff, and revert with git if you disagree. The project text above the
     block is not touched.
   - The opencode roles keep the old models with no variant, as the old bridges ran
     them; the default effort only goes with the default model (DEC-015).

3. Edit `bridge.toml`, `[project]`:
   - uncomment `phases`: `"PRD section 14"` for FillingQA, `"PRD section 9"` for the
     other two;
   - check that `supervisor_rules = "docs/SUPERVISOR.md"` is set;
   - for xbrl-frontier, `env_file = ".env"`. With the builder on opencode, the one
     opencode server gets these variables, so the opencode supervisor's tools can see
     them too, as with the old bridge.

4. Optional: an enforced read-only supervisor. Change `[supervisor]` to
   `engine = "claude-code"` and `model = "claude-fable-5-1"`. The supervisor then starts a
   fresh session (a stored session only resumes on the engine that created it); that
   costs its memory, not correctness, because it re-verifies every
   turn. On opencode, `status` reports "read-only by instruction only".

5. Run `agent-bridge check`. It makes no model calls. Fix anything it reports as a
   PROBLEM.

6. Run `agent-bridge approve --no-run`. This adopts the current `docs/PRD.md`,
   `CLAUDE.md` and `bridge.toml` as the approved contract and appends a
   `DEC-001 Existing contract adopted by the owner` entry to the ledger. That is a new
   heading style in `TRADEOFFS.md`; `review` reads both.
   - **Use `--no-run`.** Without it, a project adopted with `--adopt-legacy` starts the
     loop at once (`approve` runs until PROJECT COMPLETE by default).
   - **Expect PRD warnings.** None of the three PRDs has the headings a planner-written
     PRD has (Goals, Non-goals, Requirements with R-ids, Results, exit-criteria lists).
     `approve` prints these as warnings and adopts anyway.

7. Record the phases finished before the migration. The bridge cannot know them, so
   without this it treats Phase 1 as current. Use `agent-bridge approve --done 1 --done 2
   --reason "<where it was verified>"` (UI: `:` → Record phases finished before
   agent-bridge). Each phase gets an OWNER DECISION entry in the ledger. Only `Phase N`
   headings count, so this applies to FillingQA, whose phases are headings, and not to
   the table-based plans of the other two.

8. Start the project as described in section 3.

Changing `bridge.toml`, `CLAUDE.md` or the PRD after step 6 needs a re-approval:
`agent-bridge approve` (UI: `a`). A supervisor session reads `docs/SUPERVISOR.md` only
when it starts, so after changing it start a fresh one: `agent-bridge pin
--new-supervisor`.

## 3. Per project

### FillingQA (4096): complete at the owner-blocked boundary since 2026-10-03 03:41

- **Do not adopt the legacy state.**
  - `.bridge/unsent_reply.md` is a stale "Noted. Wait." from 2026-10-03 02:40; the run
    after it completed.
  - The builder session (`ses_f08e8379…`) reached about 968k tokens per request.
- **Fresh sessions are the better start:** run step 2 without `--adopt-legacy`. (If it is
  adopted anyway, the stale reply is now skipped because `builder_last.md` is newer.) On
  the real move it was adopted, and the stale reply was delivered (section 6).
- **Record phases 1–3** (step 7): the README's status says they are complete, WORKLOG
  shows the Phase 1 exit accepted at `8f9faed`, the Phase 2 exit met on the dev backend,
  and the Phase 3 exit statement.
- **The next owner round:**
  1. `agent-bridge review --template docs/OWNER_REVIEW2.md` lists the autonomous
     decisions still unreviewed and the OWNER-BLOCKED items.
  2. Write your D-items in that file.
  3. `agent-bridge decide docs/OWNER_REVIEW2.md`.

### xbrl-frontier (4097): stopped 2026-10-04 13:12 after three 3-hour timeouts

- The builder session went on working, unsupervised, until 17:35 that day (INVENTORY
  L2, L4). `builder_last.md` (04:11) is older than the session's real state.
- Check first whether Kaggle kernels or the E4 wrapper are still running.
- Move `.bridge/builder_last.md` aside, then run step 2 with `--adopt-legacy`. That keeps
  the builder session, which knows the Kaggle state.
- Start with an owner kickoff:

  ```
  agent-bridge run --forever --kickoff "agent-bridge replaced tools/bridge.py. Your turns on 2026-10-04 after 05:01 ran unsupervised. Before anything else, report the current state: git log since eaba7fb, every Kaggle kernel, the E4 wrapper and the GPU quota. For long jobs, end your turn with WAIT FOR PID <n> or WAIT UNTIL <time> instead of polling inside the turn."
  ```

### netcode-testbed (4098): stopped 2026-10-03 13:16; Phase 0 OWNER-BLOCKED on a quiet host

- `builder_last.md` (13:47) is the latest report, about job 84078 waiting for a quiet
  host.
- Run step 2 with `--adopt-legacy`; it becomes the supervisor's first review.
- Job 84078 was no longer running when this was written (`ps -p 84078` found nothing on
  2026-10-06), so the supervisor should first establish the job's state.
- Then `agent-bridge run --forever`.
- The builder used `sleep 590` calls inside its turns. Under agent-bridge the supervisor
  or the builder writes `WAIT FOR PID 84078` instead, and the bridge sleeps without model
  calls. If the job has ended, the wait ends at once.

## 4. What behaves differently

- **Labels.** Every message carries labelled origins (`[owner]`, `[planner]`,
  `[supervisor]`, `[bridge]`). The supervisor no longer writes "as the developer".
- **SCOPE.** The supervisor must write a `SCOPE` line. One nudge, then a warning.
- **Re-planning.** A plan problem goes to the planner (`REPLAN`). A material change waits
  for you, and a change that weakens a threshold always waits for you.
- **Waits and limits.** Waits and usage-limit resets sleep with no model calls.
- **Builder rotation.** A verified `PHASE COMPLETE` rotates the builder by default
  (`rotation.builder_on_phase_complete`).
- **The builder stays in the repo.** A builder write outside the repo pauses the run
  (L22).
- **Report.** `agent-bridge report` and the PROJECT COMPLETE report replace reading the
  logs by hand.
- **A results register.** The rules block requires every reported number to have a row
  in `docs/RESULTS.md`, marked development or real. A migrated project has none, so the
  supervisor's first task is usually to build it from the numbers already published
  (FillingQA: 201 rows). To avoid that, point `[project] results` at an existing
  register before approving.
- **Phases are not headings in xbrl-frontier and netcode-testbed.** Their phase plans are
  tables in PRD section 9; FillingQA's are `### Phase N` headings under section 14. The
  bridge finds phases only as `Phase N` headings. For X and N the supervisor works from
  `phases = "PRD section 9"`, and PHASE COMPLETE still rotates the builder and is
  recorded, but the "every phase verified" hint and the current-phase line given to the
  planner are missing. Untested on these PRDs: neither project has moved yet.

## 5. Rolling back

1. Run `agent-bridge stop` and wait for it to stop.
2. The old `tools/bridge.py` still works from the same folder. agent-bridge never edits
   `session`, `supervisor_session` or the kickoff files; it appends to the logs, and
   writes `builder_last.md` with the same meaning the old bridge expects.
3. To remove agent-bridge's own files, delete `bridge.toml` and, in `.bridge/`:
   `state.json`, `events.jsonl`, `sessions.json`, `lock`, `plan/`, `turns/`, `inbox/`,
   `reports/`, and any `report.md`, `owner_todo.md` or `PAUSED.md`. The ledger entries
   agent-bridge appended to `docs/TRADEOFFS.md` stay; revert them with git if you want.
4. Revert `CLAUDE.md` with git if you applied `--write-rules`.

## 6. What happened on the FillingQA move (2026-10-06)

From FillingQA's `.bridge/` logs, its git history and the UI's command log
(`~/.local/state/agent-bridge/ui/`). Times are local (+05:30). FillingQA's local folder is
`~/FillingQA`.

**The steps.** The owner moved it with the terminal UI. Its command log shows:
1. `init --write-rules --adopt-legacy` (15:01:34), with the default engines, then `check`.
2. `approve --no-run` (15:03:10). It adopted the contract as `DEC-001` in
   `docs/TRADEOFFS.md`.
3. `run --forever` (15:03:49).

Steps 1, 3 and 7 of section 2 were not done then: there was no `docs/SUPERVISOR.md`,
`phases` was unset, and no phases were recorded.

**Engines and sessions.**
- All three roles were on Claude Code with the default models: planner
  `claude-fable-5-1` (max), supervisor `claude-fable-5-1` (xhigh), builder
  `claude-opus-5-5`.
- Every turn was served by the configured model.
- Both roles that ran started fresh sessions. The old bridge's opencode sessions were not
  resumed.

**What the loop did,** 15:03:51 to 15:11:37:
- **Exchange 0, the builder** (32 s).
  - Its first message carried the old bridge's stale unsent reply, "Noted. Wait." from
    2026-10-03, labelled `[supervisor] (saved by the old bridge when it stopped)`.
    `--adopt-legacy` had queued it.
  - It committed the bridge's own records as `50c46d7`: the DEC-001 entry, the
    `CLAUDE.md` block and `bridge.toml`.
  - It ended with `WAIT UNTIL 2026-10-07T09:00`.
- **Exchange 1, the supervisor** (180 s, ending at 126,585 tokens of context).
  - It accepted those records. It noted that its read-only tools could not recompute the
    hashes or read git, so the hash match rested on the builder's report.
  - It wrote `NO WAIT` and gave one task: create `docs/RESULTS.md`, the results register
    that `bridge.toml` names and the rules block in `CLAUDE.md` requires.
- **Exchange 1, the builder** (254 s).
  - It committed `b609a13`: `docs/RESULTS.md` with 201 rows copied from committed run
    files, all marked `development`.
  - The same commit adds `DEC-002`: the supervisor's labelling rule, recorded as an
    autonomous decision for the owner to review.
  - It again ended with `WAIT UNTIL 2026-10-07T09:00`.
- **The stop.** The owner pressed stop at 15:09:17. The running turn finished first, and
  the bridge paused at 15:11:37, before the supervisor reviewed the second report. The
  next run starts with that review.
- `review.log` holds no alerts for the run, only the contract approval.

**Fixes after the run,** at about 15:45:
- In FillingQA (commit `99ab817`):
  - `docs/SUPERVISOR.md` with the project's rules;
  - `supervisor_rules` and `phases` set in `bridge.toml`;
  - the contract re-approved (`DEC-003`);
  - phases 1–3 recorded as complete before the move (`DEC-004` to `DEC-006`);
  - the next supervisor turn set to start a fresh session, which reads
    `docs/SUPERVISOR.md`.
- In agent-bridge (`176085f`):
  - `--adopt-legacy` skips an unsent reply older than the last builder report;
  - the UI's old-state toggle is off by default;
  - `init` picks up `docs/SUPERVISOR.md`, and `check` and every run warn when a repo with
    an old `tools/bridge.py` has no supervisor rules;
  - `approve --done`.

**Not yet seen on FillingQA,** which has not run since 15:11 (checked at 18:59):
- A supervisor turn with `docs/SUPERVISOR.md`.
- The builder under the sandbox. The sandbox was added after the run and is on by
  default, so the next run is its first.
- A planner turn.
- PHASE COMPLETE, builder rotation and PROJECT COMPLETE.
- Sleeps for WAITs or usage limits.
- `decide` (the owner round in section 3).
- `project.verify` (not set), notifications and cost tracking, all added after the run.
