# Inventory of the three `bridge.py` copies

Read-only study, 2026-10-06, of:

| Tag | File | Lines | Port |
|---|---|---|---|
| **F** | `~/FillingQA/tools/bridge.py` | 798 | 4096 |
| **X** | `~/xbrl-frontier/tools/bridge.py` | 887 | 4097 |
| **N** | `~/netcode-testbed/tools/bridge.py` | 997 | 4098 |

Also read: each project's `.bridge/` folder (`loop.log`, `review.log`, `console.log`,
`serve.log`, kickoff files, session files, `unsent_reply.md`), `CLAUDE.md` / `AGENTS.md`,
the git history of `tools/bridge.py`, and the opencode 1.18.30 binary (for its `run`
client and server routes). Where a log could not show timing, the opencode database
(`~/.local/share/opencode/opencode.db`) was queried with `sqlite3 -readonly`. Nothing in
the three projects was modified, and no process was touched.

## 0. Lineage

No copy is a superset of another.

| When (IST) | Copy | Change |
|---|---|---|
| 10-01 23:34 | F `2e13791` | `--forever`, timeout resume, shared opencode server for the live view |
| 10-02 00:49 | F (committed 15:04 as `f8cdd03`) | opencode supervisor in a persistent session, repo tripwire, served-model check. This is F's current file. |
| 10-02 01:02 | X `44b60e1` | copy of F's file |
| 10-02 01:09 | X `6387e84` | adapted: repo anchored to `__file__`, port 4097, project prompts |
| 10-02 09:42 | X `61e2025` | kickoff runs inside `--forever`; `opencode.json` denies the question tool; CLAUDE.md headless rule |
| 10-02 12:36 | N `988d079` | byte-identical copy of F's file |
| 10-02 12:39 | N (file; committed 10-05 as `7cba5b9`) | adapted: `__file__` anchoring and kickoff-in-forever (from X), port 4098, question polling, server-side abort, headless prefix |
| 10-02 17:31 | X `4b4d485` | loads `.env` into the bridge's environment (owner decision T-033) |
| 10-02 22:12 | X `d35e89e` | idle backoff: 30 min, doubling to 2 h |
| 10-03 00:10 | X `ef7976b` | fresh supervisor session after a supervisor timeout |

N lacks X's `.env` loading, idle backoff and fresh-after-timeout. X lacks N's question
polling, server-side abort, headless prefix and separate note handling. F has neither set.

## 1. Feature matrix

✓ present, — absent, ~ partial or different.

### CLI and modes

| # | Feature | F | X | N | Notes |
|---|---|---|---|---|---|
| 1 | `--check`: setup report, no model calls | ✓ | ✓ | ✓ | X/N print the full `opencode attach URL --dir REPO --session ID` command and check more project docs |
| 2 | `--pin SESSION_ID` writes `.bridge/session` | ✓ | ✓ | ✓ | the id is not validated |
| 3 | `--kickoff MSG\|@file` | ✓ | ✓ | ✓ | with `--forever`, F sends it outside the loop (an error or timeout ends the bridge); X/N make it the loop's first pending message |
| 4 | Manual mode (no flags): the supervisor drafts, the owner sends, edits in `$EDITOR`, or discards | ✓ | ✓ | ✓ | stops on a danger term or ESCALATE |
| 5 | `--show`: last builder output | ✓ | ✓ | ✓ | |
| 6 | `--loop N` (capped at 12): stops on a repeat, checkpoint phrase, ESCALATE or danger term | ✓ | ✓ | ✓ | |
| 7 | `--forever` until `PROJECT COMPLETE`; requires a pinned session | ✓ | ✓ | ✓ | |
| 8 | `--new-supervisor` | ✓ | ✓ | ✓ | |
| 9 | Configuration by environment variables (`BRIDGE_*`, `<PROJECT>_REPO`) | ✓ | ✓ | ✓ | 11 variables; X adds `BRIDGE_IDLE_WAIT` |

### Builder (opencode)

| # | Feature | F | X | N | Notes |
|---|---|---|---|---|---|
| 10 | `opencode run --attach URL --dir REPO --session ID [-m MODEL] --auto MSG` | ✓ | ✓ | ✓ | falls back to `--continue` (the server's most recent session) when unpinned |
| 11 | One `opencode serve` per project, detached so it outlives the bridge, output in `serve.log` | ✓ | ✓ | ✓ | waits 30 s for the port; never checks what is listening on it |
| 12 | Reply = stdout of the default format, ANSI stripped, last 24,000 chars, saved to `builder_last.md` | ✓ | ✓ | ✓ | |
| 13 | Guard against a message starting with `-` | ✓ `Reply:\n` | ✓ | ~ via the headless prefix | |
| 14 | Builder timeout 10,800 s, then resend with a resume note | ✓ | ✓ | ✓ | wording differs, see §2 |
| 15 | Server-side abort on timeout (`POST /session/{id}/abort`) | — | — | ✓ | F/X kill only the client |
| 16 | Question-tool guard | — | ~ `opencode.json` deny and a CLAUDE.md rule | ✓ polls `GET /question` every 15 s, rejects, aborts, resends with the questions listed | |
| 17 | Headless rule prefixed to every builder message | — | — | ✓ | |
| 18 | `.env` loaded into the bridge's environment, inherited by the server and the builder's shell | — | ✓ | — | |
| 19 | Repo anchored to the script, not the cwd | — (cwd) | ✓ | ✓ | |

### Supervisor

| # | Feature | F | X | N | Notes |
|---|---|---|---|---|---|
| 20 | Backend switch `BRIDGE_SUPERVISOR=opencode\|claude` | ✓ | ✓ | ✓ | default opencode |
| 21 | opencode: persistent session, `-m anthropic/<model>`, `--format json`, `--auto`, `--variant`, titled `<project> supervisor <time>` | ✓ | ✓ | ✓ | |
| 22 | claude: one-shot `claude -p --append-system-prompt … --tools Read,Grep,Glob`, `ANTHROPIC_API_KEY` stripped, `CLAUDE_CODE_EFFORT_LEVEL`, "ultrathink" prefix | ✓ | ✓ | ✓ | no memory between turns |
| 23 | Role, absolute repo path and read-only rules in the first message; short reminder every turn | ✓ | ✓ | ✓ | X/N's reminder names the project and path |
| 24 | Re-verify prefix every turn ("memory is context, not evidence") | ✓ | ✓ | ✓ | |
| 25 | Reply = text of the last non-empty step | ✓ | ✓ | ✓ | |
| 26 | One in-session nudge when a turn ends with no text | ✓ | ✓ | ✓ | |
| 27 | Repo tripwire (hash of HEAD, porcelain status and diff) around opencode supervisor turns, to `review.log` | ✓ | ✓ | ✓ | |
| 28 | Served-model check via `sqlite3 -readonly`, to `review.log` | ✓ | ✓ | ✓ | opencode only |
| 29 | Supervisor timeout 1,800 s | ✓ | ✓ | ✓ | |
| 30 | Fresh supervisor at a phase boundary (checkpoint phrase in the builder's report) | ✓ | ✓ | ✓ | |
| 31 | Fresh supervisor after a supervisor timeout | — | ✓ | — | rests on a misdiagnosis (L3) |
| 32 | Output protocol VERDICT / REPLY / ESCALATE; never write PROCEEDING or DECISIONS NEEDED | ✓ | ✓ | ✓ | N adds "answer the builder's questions; the bridge aborted its question tool" |
| 33 | Autonomous mode: never escalate, AUTONOMOUS DECISION entries, OWNER-BLOCKED items, phase order, completion rule | ✓ | ✓ | ✓ | project lists differ, see §2 |
| 34 | Dependency-approval policy | — | ✓ | ✓ | |

### Loop

| # | Feature | F | X | N | Notes |
|---|---|---|---|---|---|
| 35 | Sentinel on any line, `*#\`` stripped, so a preamble is tolerated | ✓ | ✓ | ✓ | also fires inside the REPLY body |
| 36 | Danger terms: stop (`--loop`, manual) or log to `review.log` (`--forever`) | ✓ 17 | ✓ 17 | ✓ 25 | substring match on prose |
| 37 | Checkpoint phrases: stop (`--loop`) or log (`--forever`) | ✓ | ✓ | ✓ | substring match; `blocked` matches every `OWNER-BLOCKED` |
| 38 | Repeat detection: same tail as last turn, so prepend "Take the next concrete unblocked task from PRD section N" | ✓ | ✓ | ✓ | project text hard-coded; unlabeled |
| 39 | Idle backoff: reply under 600 chars and repo unchanged twice in a row, then sleep 30 min doubling to 2 h | — | ✓ | — | |
| 40 | Error backoff 60 s doubling to 1,800 s, STOP-aware sleep | ✓ | ✓ | ✓ | retries any error forever |
| 41 | STOP file; unsent reply saved | ✓ | ✓ | ✓ | lost on one path (L13) |
| 42 | `loop.log` transcript, `review.log` warnings | ✓ | ✓ | ✓ | |
| 43 | `BRIDGE_EFFORT` validation (`max`, `ultra` map to `xhigh`) | ✓ | ✓ | ✓ | |
| 44 | `AGENTS.md` check with symlink advice | ✓ | ✓ | ✓ | |

### Outside the script

| # | Feature | F | X | N | Notes |
|---|---|---|---|---|---|
| 45 | `caffeinate -is` | launch command | launch command | launch command | not in the script |
| 46 | Relaunch after a job: `relaunch_after_eval.sh` (`while kill -0 64410; do sleep 60; done`) | ✓ | — | — | a hand-built wait primitive |
| 47 | Owner decisions doc, short kickoff, `--new-supervisor` | ✓ `docs/OWNER_REVIEW.md` | ✓ kickoffs 2–4, T-033 in CLAUDE.md | — | done by hand |

## 2. Divergences, and which version is better

1. **Repo anchoring: X/N.** F uses the cwd, so running it from another folder drives
   another project. agent-bridge: an explicit config, an absolute repo path, and a
   per-project lock.
2. **Kickoff inside `--forever`: X/N.** In F, a kickoff timeout or error ends the bridge.
   That is exactly what happened in X before `61e2025` (L5).
3. **Timeout handling: N.** N aborts the server-side turn. F/X kill only the client, the
   server keeps running the turn, and the resend lands as a queued message (L4).
4. **Resume-note bookkeeping: N.** N keeps a separate `note` that is reset after a
   success. F/X prepend the note into `pending` with a `startswith` guard.
5. **Question guard: N, then X, then F.** X's `opencode.json` deny does not hold under
   the claude-bridge plugin (N's own comment; consistent with the plugin overriding
   permission denies). N's polling does not depend on denies, but it never fired in N's
   logs, so it is untested in practice.
6. **Idle handling: X, as a backstop only.** After `d35e89e` X's idle stretches shrank to
   about 4 exchanges between sleeps. It still guesses (30 min to 2 h) when the end was
   known: a pid (64410) or a time (the 07:00 UTC quota reset). The real fix is a wait
   primitive.
7. **Fresh supervisor after a timeout (X): worse.** It rests on a misdiagnosis (L3) and
   discards the supervisor's memory every time a usage limit hits.
8. **`.env` loading (X): needed but too implicit.** X's builder needs `KAGGLE_API_TOKEN`
   and `GEMINI_API_KEY`. Loading `.env` into the bridge leaks every key to every
   session on that server. Port it as an explicit opt-in.
9. **Danger terms: F/X, but both are noisy.** N's additions (`postgres`, `git push`)
   only added noise: 13 `postgres` hits, all prose. Match executed commands, not prose.
10. **Prompts: no "better" version.** The project-specific `SYSTEM` and `AUTONOMOUS`
    text is the main divergence and the reason for three copies. It must move into repo
    docs and config.
11. **`--check` output: X/N.** They print the full attach command.

## 3. Failures found in the logs

Each entry: what happened, the evidence, the cause, where it was fixed (if anywhere),
and what agent-bridge must do.

**L1. Idle polling.**
- F: the run began 10-02 18:28. In exchange 3 (22:20) the builder scheduled the eval
  resume as pid 64410 for the 02:41 limit reset and wrote "Send 'continue' after about
  03:45 IST". Exchanges 4–89 (86 exchanges, 22:21 to 00:35, plus one at 02:40) were
  "No change" / "Noted. Wait." The opencode DB shows 172 builder model requests in
  22:20–00:36 (API-equivalent cost $41.67 by opencode's accounting), all on the plan
  the eval needed. The owner stopped it at 02:40 (`unsent_reply.md` is "Noted. Wait.")
  and relaunched by hand with `relaunch_after_eval.sh` and `continue_kickoff.md`.
- X: exchanges 6–92 (87 exchanges, 10-02 19:16 to 22:11) waited for a known time, the
  Gemini quota reset at 07:00 UTC. The supervisor wrote "Stop replying until…", which
  the bridge cannot honour: it always sends the next message.
- N: idle bursts (exchanges 5–12, 15–25). The builder then moved the polling into its
  turn: `sleep 590` tool calls for up to about 110 minutes (`builder_last.md`), each a
  model request with the full context. N's quiet-host check meanwhile kept failing on
  "another agent's opencode process at 46% CPU".
- Fixed: partly, by X's idle backoff. Missing: a wait primitive (WAIT UNTIL / FOR PID /
  FOR FILE) that sleeps with no model calls.

**L2. Usage limits invisible to the bridge.** The account-pool plugin answers HTTP 429
with `retry-after` set to the seconds until the earliest account resets. opencode
honours it and keeps the session in status `retry`; `opencode run` only exits when the
session goes idle; the bridge sees a hang.
- F supervisor: the 22:33:01 turn ran one `read`; the next model request waited until
  00:10:04. The bridge logged three 1,800 s timeouts (23:02, 23:33, 00:05) and backed
  off 60/120/240 s. `serve.log`: "Claude pool exhausted — next account free in 1h 37m".
- X supervisor: the same pattern, 22:47:56 to 00:10:05 (timeouts at 23:17 and 23:48).
- X builder on 10-04: last request 05:01:26, next 17:30:12, a 12 h 29 min stall matching
  `serve.log` ("resets in 44912 seconds", "next account free in 12h 29m"). The bridge
  timed out at 07:12, 10:12 and 13:12.
- `claude -p` reports the same condition as an error result: "You've hit your session
  limit · resets 9:40pm (Asia/Calcutta)" (FillingQA's own eval,
  `eval/runs/7a079fe7abfe.errors.jsonl`).
- Fixed: nowhere. agent-bridge: read opencode's session status (`retry` with its `next`
  time) and claude's limit messages, abort the turn, and sleep until the reset.

**L3. Misdiagnosed supervisor timeouts (X `ef7976b`).** The commit starts a fresh
supervisor after a timeout because "the session's context has grown too large". The
session peaked at 150,841 tokens, and the model answered in 4 s whenever an account was
free. The cause was L2. Not to be ported.

**L4. Server-side turn left running after a client timeout (F, X).** Retries became
queued user messages in a busy session: the F supervisor at 23:04:00 and 23:36:00, the
X builder on 10-04 at 07:12:09 and 10:12:09. X's builder resumed at 17:30, four hours
after the bridge had stopped, and worked unsupervised until 17:35 with two stacked
resume notes. Fixed: N (abort). agent-bridge: abort server-side before every resend,
and never send to a busy session.

**L5. Question-tool hang (X, 10-02 01:11).** The builder called opencode's `question`
tool ("Phase 0 dependencies for .venv … Which do you approve?"). The status stayed
`running` for 3 hours. The kickoff ran outside `--forever`, so the timeout ended the
bridge, and the owner wrote `resume.md` by hand. Three causes:
- The pinned session had been created in the TUI. In 1.18.30, `opencode run` adds
  `question` / `plan_enter` / `plan_exit` deny rules only to sessions it creates
  (verified in the binary).
- `opencode run` does not handle question events at all.
- X's CLAUDE.md said "Ask before adding any dependency … and wait for approval".
Fixed: X (CLAUDE.md headless rule, `opencode.json` deny) and N (polling, abort,
headless prefix). agent-bridge: create builder sessions with `opencode run` (so they get
the deny), poll for questions, keep the headless rule in every message, and for
`claude -p` disallow `AskUserQuestion` and set `--permission-prompts none`.

**L6. An opencode upgrade broke the flags (F, 10-02 12:13–12:20).** "Unrecognized flag:
--attach / --dir in command opencode run", retried 4 times with 60–480 s backoff.
Fixed outside the script (pinned 1.18.30, `autoupdate: false`). Missing: a version check
at startup, and treating flag errors as fatal, not transient.

**L7. Supervisor without `--auto` (F, 10-02 00:48).** "The user rejected permission to
use this specific tool call": `opencode run` auto-rejects permission prompts unless
given `--auto`. Fixed in all three.

**L8. Tests wrote into the live log (F).** `loop.log` contains fake exchanges ("same
output", "watch the password in the DSN", a fake `PROJECT COMPLETE`) at 10-01 16:31:28,
16:31:51, 20:19:44 and 23:24:12. The bridge's ad-hoc tests used the real `.bridge/`.
agent-bridge: every test runs in a temporary state directory.

**L9. Keyword noise in `review.log`.** `blocked` matched every `OWNER-BLOCKED`: 28
checkpoint entries in F, 11 in X, 36 in N. `postgres` produced 13 hits in N, and
`truncate` matched F's "never truncate". The real signals (tripwire, model mismatch)
drown. agent-bridge: structured signals, and danger patterns matched against executed
commands.

**L10. Tripwire false positive (N, exchange 2).** "SUPERVISOR CHANGED THE REPO" listed
`tools/bridge.py`, `docs/config/experiment.yaml` and the untracked `CLAUDE.md` /
`AGENTS.md`. Those are the owner's own edits made during that turn (the adapted bridge
and rules were committed later). A whole-repo hash cannot attribute a change, and
builder background jobs also write during supervisor turns. agent-bridge: log the delta
(which paths changed), and audit the supervisor's actual tool calls.

**L11. An owner kickoff overridden by the supervisor (X, 10-02 22:12–22:17).** The
owner's kickoff3 told the builder to re-push the GPU check "at most once every 2 hours".
The supervisor never sees kickoffs; at 22:16 it called this "a self-invented two-hour
re-push cadence that nobody instructed" and overrode it. The builder replied that it
"came from the bridge's status message" and dropped it. The owner's instruction was
lost. agent-bridge: owner messages go verbatim to both agents, labelled as the owner's.

**L12. Bridge text taken for an owner instruction.** The repeat detector's boilerplate
("Take the next concrete unblocked task from PRD section 14 and docs/OPEN.md") is
unlabelled. In the FillingQA owner review (10-02) the owner had to say it was not from
them. agent-bridge: every injected line is labelled `[bridge]`, and no project task text
is hard-coded.

**L13. Unsent reply lost on one STOP path.** When STOP appears during a builder turn
that then times out, the loop `continue`s and exits at the `while` test without saving
`pending` (X, 10-04 13:12: no `unsent_reply.md`). The same code is in F and N.

**L14. Repeated timeouts never escalated (X, 10-04).** Three consecutive 3-hour
timeouts (9 hours) with resends. Nothing paused, and nothing was summarised for the
owner.

**L15. Supervisor anchored after completion (F).** After F's `PROJECT COMPLETE`
(10-02 13:18, exchange 24), the owner-review relaunch needed `--new-supervisor`, and so
did the 10-03 03:31 relaunch. Fixed: by a manual flag. agent-bridge: a completed
supervisor session is closed, and new work always starts a fresh one.

**L16. Console history lost (X).** X's `console.log` starts at the 10-03 00:10 relaunch
("supervisor session cleared"): the launch command truncated it. agent-bridge: the
bridge writes its own append-only console log, with launch markers.

**L17. State copied across projects (X).** `.bridge/_filingqa_copy/` holds FillingQA's
`session` and `supervisor_session` from when the script was copied. Left in place, X's
bridge would have driven FillingQA's builder: the server accepts any session id. agent-
bridge: check that a pinned session's directory is this repo.

**L18. Shared-login logouts.** F's `serve.log` shows 9 warnings that a pool account
"shares one login with Claude Code on this machine; whichever refreshes first logs the
other out". The bridges had no 401 handling, so they would have retried forever.
agent-bridge: detect auth failures and pause with "run `claude login`".

**L19. Context growth.** F's builder session (2,086 messages) peaked at 967,782 input
tokens per request; X's builder ran at about 420k on 10-04. Every idle exchange
re-sent that context. agent-bridge: rotate the builder session with a handoff from the
repo docs.

**L20. The 3-hour cut pushes waiting into the turn.** The builders plan around the cut:
"Polling for four hours in this turn would likely hit the bridge timeout again, so I'm
stopping here" (F); X's kickoff4: "poll inside your turn for up to about 2 hours".
Same root as L1.

**L21. opencode server warnings, low severity.** `MaxListenersExceededWarning: Possible
EventTarget memory leak` appears in all three `serve.log`s (many runs attached to one
long-lived server). "OPENCODE_SERVER_PASSWORD is not set; server is unsecured": the
server is bound to localhost, but any local process can drive an `--auto` builder.

### Latent defects in the code (not observed in the logs)

- **C1.** The phase-boundary `fresh` flag is recomputed on every retry of a supervisor
  draft. While the builder's last report contains "phase N complete", each retry opens
  another supervisor session.
- **C2.** No single-instance lock. `--forever` deletes STOP at start, so a second launch
  within 5 s of a STOP can leave the first instance running.
- **C3.** An unpinned builder uses `--continue`: the server's most recent session, which
  may be another conversation.
- **C4.** The sentinel is matched anywhere, including inside the REPLY body (for example
  a quoted rule).
- **C5.** With no `REPLY:` marker, the whole raw supervisor output is sent to the
  builder.
- **C6.** Repeat detection uses `hash()`, which is per-process and does not survive a
  restart (minor).

## 4. How project rules reached the agents

- **Builder.** `AGENTS.md` is a symlink to `CLAUDE.md` in all three projects. opencode
  loads `AGENTS.md`, plus the global `~/.config/opencode/AGENTS.md`, into every session.
  Owner decisions were added to `CLAUDE.md` mid-run (X's "Kaggle and frontier calls"
  section, the headless rule) or sent as kickoff files.
- **Supervisor.** Its role and principles are hard-coded in each `bridge.py` (`SYSTEM`
  plus `AUTONOMOUS`, 60–140 lines of project text). The opencode supervisor runs in the
  repo directory, so opencode also injects the builder's `AGENTS.md` into its context.
  Hence the opening "ROLE FOR THIS ENTIRE SESSION. Ignore any instruction to implement…".
  The claude supervisor ran with cwd set to the repo, so Claude Code loaded `CLAUDE.md`
  as well.
- **Report format.** `DECISIONS NEEDED:` and `PROCEEDING:` are defined in `CLAUDE.md` for
  the builder; the supervisor is told never to use them.
- **Wording matters.** FillingQA's `CLAUDE.md` is written for an interactive developer
  ("Ask before adding a dependency", "Stop and ask if something in the PRD looks
  wrong"). X inherited that wording, and it led to L5.

## 5. What worked and must be kept

- The two-agent shape: a builder that does the work, and a supervisor that verifies
  against the repo and writes the next instruction.
- Persistent sessions with a per-turn "re-verify, memory is context, not evidence".
- Absolute paths for the supervisor.
- The autonomous rules: AUTONOMOUS DECISION entries for later owner review, OWNER-BLOCKED
  items skipped, completion at the owner-blocked boundary. Both real completions put the
  sentinel on the first line (F 10-02 13:18 and 10-03 03:41, X 10-02 15:01).
- The owner loop: owner decisions in `docs/OWNER_REVIEW.md` (D1–D6 with a "Done when"
  checklist), a short kickoff pointing at it, and a fresh supervisor. FillingQA went from
  owner review to the owner-blocked boundary in one unattended run.
- Pinned opencode 1.18.30, one server per project on its own port, and
  `opencode attach` as the live view.

## 6. Failures found while building agent-bridge

**L22. An opencode builder worked in another repository (live smoke test, 2026-10-06
02:41).**
- **Setup:** all three roles on opencode 1.18.30 with Haiku. The server was started by
  the bridge with cwd = the temporary repo, and every turn passed `--dir <temp repo>`.
- **What happened:** in its first turn the builder treated
  `~/src/opencode-claude-bridge` (the claude-bridge login wrapper's directory) as its
  working directory. It created five contract files there and committed them as
  `22af7c8` on that repo's branch `fix/ec2-audit-hardening`, then wrote two more files.
  Its report claimed the work was done in the project. The supervisor checked the temp
  repo, found no commits, and called the report false.
- **Why:** the same wrapper behaviour the hard-won notes describe for the supervisor
  ("resolves relative paths against its own working directory"). The builder had no
  absolute-path rule; only the supervisor did.
- **Repair (2026-10-06 02:50):**
  - the commit is kept on a backup branch `agent-bridge-smoke-accident-20261006`;
  - the branch tip was moved back with `git reset --soft` to the owner's commit;
  - the five files were unstaged, and the seven files plus `.pytest_cache` were moved to
    `/tmp/agent-bridge-smoke-accident-backup-20261006/`;
  - the owner's uncommitted changes were not touched, and the commit was never pushed.
- **Fix:**
  - every builder message now names the repo and requires absolute paths and
    `cd <repo> &&`;
  - a post-turn audit pauses the run when the builder writes under `/Users/` outside the
    repo, and reports reads there.
