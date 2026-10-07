# Decisions

The decision ledger for agent-bridge itself, in the format of docs/DESIGN.md 6.5.
Entries made during the overnight build of 2026-10-06, without the owner, carry
"AUTONOMOUS DECISION - owner to review".

## DEC-001 An opencode role needs an explicit port
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN v3 showed `# port = 4096` as a commented default. Options:
- **Default to 4096.** Simple, but a new project would attach to FilingQA's server
  (4096) and drive its sessions in that server's environment.
- **Require `[opencode] port` whenever a role uses opencode.** `init` picks a free port.

Chosen: require it. A wrong default fails silently; a missing value fails loudly.

## DEC-002 Every project path must resolve inside the repo
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

The planner writes `bridge.toml`, and the bridge writes the files it names. A `prd` of
`../../etc/x` would let a planner direct the bridge outside the repo. Options:
- trust the paths;
- reject only absolute paths;
- reject anything that resolves outside the repo.

Chosen: the third, at config validation, for every `[project]` path.

## DEC-003 Directive keywords are case-sensitive, after stripping markdown
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

The parsers match `VERDICT:`, `REPLY:`, `SCOPE:`, `REPLAN`, `KICKOFF:` and the rest in
upper case, after removing bold, backticks, headings and bullets. Case-insensitive
matching would turn prose such as "Summary: …" inside a ledger body into a new
section. The old bridges also matched `REPLY:` exactly.

## DEC-004 Numbered phases only, for blocking
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

SCOPE, AFFECTS and the blocked set compare normalized tokens: ids such as `R-4` or
`F-59`, and numbered phases such as `phase 3a`. A named phase ("Phase Alpha") is not
recognised as a token. Matching free text would block on stray words. Planner-written
PRDs number their phases, and the PRD check enforces it.

## DEC-005 "Weakens" is detected conservatively
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN 6.6 says a change that weakens an exit criterion or a threshold always waits for
the owner, but the planner cannot be trusted to flag it. The bridge treats a PRD edit as
weakening when either:
- it changes or removes any number; or
- it changes or removes a line of an exit-criteria list.

Adding a criterion is material but not weakening. Options:
- trust the planner's flag;
- parse the comparisons ("≥ 0.90" vs "≥ 0.80");
- the conservative textual rule above.

Chosen: the textual rule. It over-flags, for example renumbering, but a false alarm only
costs the owner a look. A miss would let a phase pass by moving its bar.

## DEC-006 Contract drift is attributed per turn
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

Comparing the contract against the approved hashes after each builder turn would blame
the builder for the owner's own edits between runs. The bridge now compares the hashes
before and after each builder turn; only a change inside the turn is "changed by the
builder". A change found at `run` start is reported as "outside a turn", and
`agent-bridge approve` re-approves the current files. DESIGN 6.8 is updated.

## DEC-007 Phase completions in the ledger get their own status
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN 6.5 had no status for the entry recorded at `PHASE COMPLETE`, and none of the
listed statuses fit, since it is neither a proposal nor an owner decision. Added:
`VERIFIED (phase boundary)`, with "Decided by: supervisor (verified at exchange N)".

## DEC-008 Owner decisions supersede pending work
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

When `decide` arrives, a builder report waiting for review is not reviewed, and an
active wait is dropped. The fresh supervisor sees the builder's next report. Both are
logged as events. Options:
- finish the pending review first;
- queue the decisions until after the wait;
- supersede (chosen).

The owner's decisions change the plan the review would have been judged against.

## DEC-009 A failed re-plan leaves the plan unchanged
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

If the planner's answer to a supervisor REPLAN fails the bridge's checks twice (for
example a FIND that does not match), the supervisor is told the plan is unchanged and
continues under the current contract; the failure goes to `review.log`. For `new` and
`decide`, where the owner is waiting on the result, the run pauses instead.

## DEC-010 The planner always interviews first
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN v3 let the planner skip the questions and return PLAN directly. In the first live
smoke test, Haiku did exactly that and returned the project's code files
(`wordcount.py`, `test_wordcount.py`) instead of the contract. The bridge's allowlist
refused them, but the turn was wasted. The interview now accepts only QUESTIONS, which is
also what the owner specified ("interviews me with a short batch of numbered
questions"). The drafting instructions now carry an exact skeleton for each file.

## DEC-011 bridge.toml is optional in the planner's PLAN
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

The bridge writes a valid provisional `bridge.toml` from the `new` flags before the
planner runs. In the live test, Haiku invented its own format (no `version`, unknown
`roles` and `description` keys), and that failed the whole plan twice. The planner now
returns `bridge.toml` only to change budget or rotation values, as an edit of the current
file. Engines and models stay exactly as the owner gave them either way.

## DEC-012 approve refuses unfinished planning, and adoption needs the files
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

Found live: after the planner's draft failed the checks, `approve` took the "adopt an
existing contract" path and recorded hashes of files that did not exist. Two changes:
- `approve` now refuses while planning is unfinished and says how to continue;
- adopting requires the PRD and the rules to exist.

`say` now answers the planner whenever it waits for the owner. That includes a draft
paused on failed checks, where `say` previously queued the answers as a message for the
builder.

## DEC-013 A builder write outside the repo pauses the run
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

After the L22 incident, the bridge checks the builder's tool calls after every turn.
- **"Outside"** means a path under `/Users/` or the owner's home that is not inside the
  repo. `/tmp` and system paths such as `/opt/homebrew/bin/python3.12` are not flagged.
- **Writes** pause the run: write or edit tools, and shell commands that commit, add,
  reset, check out, move, remove, copy, `sed -i` or redirect.
- **Reads** go to `review.log` only.

Options:
- block before the turn (impossible: the bridge only sees tool calls after they ran);
- report only;
- pause on writes (chosen).

The pause comes after the damage, but it stops a second turn from making it worse, and
`PAUSED.md` names the paths. The absolute-path line in every builder message is the
preventive half.

## DEC-014 The bridge says when every phase is verified; .omo/ is tool state
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

Two findings from the live runs:
- **Completion hint.** After verifying the only phase, the Haiku supervisor asked to
  re-plan a "Phase 2" the PRD never had, instead of writing PROJECT COMPLETE. The bridge
  now tells the supervisor, as a fact, when every PRD phase has a verified PHASE
  COMPLETE. It is a hint; the supervisor still decides.
- **`.omo/` is tool state.** The omo plugin writes `.omo/run-continuation/<session>.json`
  into the repo during any opencode turn, which tripped the supervisor tripwire. `.omo/`
  is excluded from repo snapshots like `.bridge/`, and `init` / `new` add it to
  `.gitignore` when a role uses opencode.

## DEC-015 The default effort goes only with the default model
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN v3 gave the planner `max` and the supervisor `xhigh` effort by default. Effort
levels are model-specific, so `init --supervisor opencode:anthropic/claude-fable-5-1`
would have passed an effort that model may not take, and the old bridges ran the opencode
supervisor with no variant. Options:
- always apply the role default;
- apply it only with the default engine and model (chosen);
- never set a default.

Done in 95e4459 (template) and 2f03232 (loader). 95e4459 was committed with its new test
failing, because a piped pytest hid the exit code; 2f03232 fixed the loader and the test.
DESIGN section 9 now says this.

## DEC-016 Migration: init --adopt-legacy, and sessions follow their engine
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN v3 section 16 migrated state with `pin`. Three additions:
- **`init --adopt-legacy`** does the migration in one step:
  - it pins the old `session` and `supervisor_session` (only for roles on opencode);
  - the old `unsent_reply.md` becomes the next builder message;
  - otherwise `builder_last.md` becomes the next report for the supervisor.
  `pin` still works.
- **An adopted supervisor session gets the role text once.** It never saw the labels,
  SCOPE, WAIT or REPLAN, and would have been nudged for SCOPE on every turn. Options:
  start a fresh supervisor (loses its memory), resend the role on every turn (cost), or
  once (chosen).
- **A role whose engine changes starts fresh.** Moving the supervisor from opencode to
  Claude Code in `bridge.toml` would otherwise have resumed an opencode id with `claude
  --resume`. A stored session now resumes only on the engine that created it.

DESIGN sections 11 and 16 are updated.

## DEC-017 A terminal UI as the one entry point
- Decided by: owner (2026-10-06, in conversation)
- Status: OWNER DECISION

The owner chose:
- a terminal UI (curses, no new dependencies) over a browser app or a native window;
- `agent-bridge` opens the current folder's project, and ctrl-a shows all projects;
- the UI can do everything the CLI does;
- `agent-bridge` is linked onto the PATH (`~/.local/bin/agent-bridge`).

## DEC-018 How the terminal UI is built
- Decided by: build agent (2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

- **Actions are CLI commands.** Each one runs an `agent-bridge` subcommand in a detached
  process, instead of calling the engine inside the UI process.
  - The CLI stays the one tested implementation.
  - A run outlives the UI.
  - A failure shows as the command's own last output line.

  Cost: one Python start per action, about a second.
- **The project list is a registry** that the CLI writes on every command. Scanning the
  disk for `.bridge/` folders would be slow and would find folders that are not
  projects.
- **No mouse support**, so the terminal's own text selection keeps working.
- **Text for the CLI goes through `@file`** (messages, answers, kickoffs, ideas). The
  files stay under `~/.local/state/agent-bridge/ui/` (the newest 200), so what was sent is
  on record.

## DEC-019 Migration fixes found on the FilingQA move
- Decided by: build agent (2026-10-06, at the owner's request)
- Status: AUTONOMOUS DECISION - owner to review

The owner migrated FilingQA with the terminal UI. The check found four gaps:
- **A stale unsent reply was delivered.** The old bridge's "Noted. Wait." from 10-03
  02:40 became the first builder message, and the builder planned to wait until the next
  morning.
  - `--adopt-legacy` now skips an `unsent_reply.md` that is older than `builder_last.md`:
    the old bridge ran past it.
  - The UI's old-state toggle is now off by default.
- **The project's supervisor rules were never set up.** `check` and every run now warn
  when a repo with an old `tools/bridge.py` has no `supervisor_rules`, or when that file
  is missing. `init` sets `supervisor_rules` when `docs/SUPERVISOR.md` exists, and the UI
  setup form says to create it first.
- **Phases finished before the migration were unknown,** so the bridge took Phase 1 as
  current. `approve --done PHASE` records them.
  - Options: let the supervisor re-verify old phases (each PHASE COMPLETE rotates the
    builder, and the old exits are not re-runnable cheaply); edit `state.json` by hand;
    an owner record (chosen).
  - Each record is an OWNER DECISION ledger entry, worded as the owner's word and never
    as verified. It is refused while a bridge runs.
- **The results register surprise** is documented in MIGRATION.md, not changed. The
  supervisor's reading, that numbers already published need rows, is a fair reading of
  the contract.

## DEC-020 Hardening after the 2026-10-06 audit
- Decided by: build agent (2026-10-06, at the owner's request)
- Status: AUTONOMOUS DECISION - owner to review

An outside audit reproduced 12 bugs and listed gaps and improvements. Each was confirmed with the auditor's
probes before it was fixed (DESIGN 8.4, `tests/test_audit.py`). The choices worth reviewing:
- **Ending the agent.** On any interrupt, the agent's whole process group is ended, including background
  jobs it started during that turn. This is the same as a timeout already did. A long job meant to
  outlive a turn must be started in its own session (`setsid` or `nohup … &` from a detached shell).
- **The leftover check** ends a process only when both its pid and its start time match the record, so a
  reused pid is never touched.
- **The sandbox** is on by default (`safety.sandbox = "auto"`) for a Claude Code builder on macOS.
  - Options were Claude Code's own sandbox setting, which covers only its Bash tool, or `sandbox-exec`
    around the whole process (chosen: it also covers Write, Edit and every child process).
  - The allow-list is generous for tool state (`~/.claude`, caches, `~/.docker`, `~/.gnupg`, …).
  - A project that needs more lists it in `sandbox_writable`; `off` is an owner-only setting.
- **The wrote-elsewhere check** adds a note for the supervisor and does not pause the run. Writes to
  ignored files, or a commit with nothing to commit, would raise false alarms.
- **Near-misses** get one nudge and are then taken as parsed, with a `review.log` entry. They are never
  accepted silently, and the run never pauses for them.
- **The cost figures** are each engine's API-price estimate. On a subscription they are a usage proxy,
  not a bill, and the cap is per `run`, like the other budget caps.
- **Notifications** are on by default on macOS. Tests switch them off with `AGENT_BRIDGE_NO_NOTIFY=1`.

## DEC-021 The ledger is append-only for the planner
- Decided by: build agent (2026-10-07, at the owner's request)
- Status: AUTONOMOUS DECISION - owner to review

A second audit showed that the guard on the planner's ledger edits compared only the set of Status and
Decided by lines in the whole file: swapping two entries' statuses, or rewriting the body of an
owner-decided entry, passed and applied without the owner.
- **The decisions ledger** is append-only for a plan change: the new text must begin with the old one.
  The planner adds entries after the last and changes none, neither bookkeeping lines nor bodies. A later
  decision supersedes an earlier one with a new entry.
- **The open items** stay editable, because they are a working list, but each entry's Status and
  Decided by lines must stay under that entry's heading. Only the owner's own decisions (`decide`) may
  change them.
- **A prefix check** rather than a per-entry comparison for the ledger: FilingQA's `TRADEOFFS.md` has
  entries in more than one heading style, and a prefix check does not depend on any of them.

## DEC-022 The owner's inbox and waiting plan changes live outside the repo
- Decided by: build agent (2026-10-07, at the owner's request)
- Status: AUTONOMOUS DECISION - owner to review

The second audit showed that a builder could write `.bridge/inbox/`, which is inside the repo and so
writable in the sandbox, and reach both agents as `[owner]`, or approve its own plan changes. Checking it
showed the same for `.bridge/plan/changes/`: approval applied whatever edits that file held.
- **The inbox** moved to a per-repo folder under `~/.local/state/agent-bridge/repos/`
  (`$XDG_STATE_HOME` if set). The sandbox does not let the builder write there; a live `sandbox-exec`
  check got "Operation not permitted". The old `.bridge/inbox/` is no longer read, and nothing in it is
  carried over, because anything there may have come from the builder.
- **Waiting plan changes** keep a copy of what the planner proposed in the same folder. Approval refuses a
  change whose `.bridge/plan/changes/` files differ from that copy, because the owner may have read the
  altered version. Rejection still works, and uses the copy.
- **A builder running an owner command** (`agent-bridge say`, `approve`, `decide`, `engines`, `init`,
  `new`, `pin`, `stop`) is logged as `DANGER COMMAND`. An unsandboxed builder can still do anything the
  owner can; the log only makes it visible.
- **The rest of `.bridge/`** stays in the repo for now (OPEN-006).

