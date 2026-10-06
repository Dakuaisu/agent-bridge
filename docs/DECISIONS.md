# Decisions

The decision ledger for agent-bridge itself, in the format of docs/DESIGN.md 6.5.
Entries made during the overnight build of 2026-10-06, without the owner, carry
"AUTONOMOUS DECISION - owner to review".

## DEC-001 An opencode role needs an explicit port
- Decided by: build agent (overnight build, 2026-10-06)
- Status: AUTONOMOUS DECISION - owner to review

DESIGN v3 showed `# port = 4096` as a commented default. Options:
- **Default to 4096.** Simple, but a new project would attach to FillingQA's server
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
