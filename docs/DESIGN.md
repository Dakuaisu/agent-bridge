# agent-bridge design

Status: v3 (2026-10-06), approved for building. Revisions:
- v2: `bridge.toml` read with `tomllib`; Claude Code as the default supervisor; labelled
  origins; a third role, the planner.
- v3:
  - what a waiting plan change blocks, and the re-plan cap pausing its phase (6.6);
  - Claude Code as the default builder, with the Opus id checked against what the
    engine serves (section 9);
  - the owner-review report (6.10);
  - builder rotation at verified phase boundaries (7.3);
  - `claude auth status` treated as a pre-check only (4.3).

The failure IDs (L1–L21, C1–C6) refer to `docs/INVENTORY.md`.

## 1. Goals

- One tool, installed once, that plans, builds and supervises work in any repo: one
  `bridge.toml` per project, no per-project Python.
- Three roles (planner, supervisor, builder). Each runs on either engine (opencode
  1.18.x or Claude Code) with its own model, in any combination.
- The plan is the contract. The owner approves `docs/PRD.md` and `CLAUDE.md` before
  anything is built. After that the plan changes only through the planner, and
  materially only with the owner.
- An honest audit trail. Every message records who wrote it, every decision records who
  made it, and the logs keep both.
- Safe to leave alone for days. It never loops on a hard error, never makes model calls
  just to pass time (L1), and every stop, wait, pause and warning shows in `status` and
  the logs.
- Everything that worked in the three copies is kept (INVENTORY §5). Every failure in
  INVENTORY §3 has a mechanism and a test.
- Zero runtime dependencies.

Non-goals: a TUI, several builders, remote machines, managing opencode installs, and
deleting sessions (the tool records the sessions it creates and never deletes one).

## 2. Roles and origins

| Role | Job | Writes | Read-only on Claude Code | Read-only on opencode |
|---|---|---|---|---|
| **planner** | interviews the owner; writes the contract; answers re-plan requests and owner decisions | only the planning docs, and only through the bridge (section 6) | enforced: it gets no write tools | read-only by instruction only |
| **supervisor** | reviews every builder turn against the contract and the repo; writes the builder's next instruction | nothing | enforced: `--tools Read,Grep,Glob` | read-only by instruction only |
| **builder** | does the work: code, tests, commits, ledger entries, open items | the repo, except the contract files | — | — |

- Read-only is a property of the role, not a setting: a writable planner or supervisor
  would break the audit model. `check`, `status` and the start banner print how it is
  enforced for each role. On opencode the text is exactly "read-only by instruction
  only", with the tripwire and tool-call audit running behind it.
- The bridge itself writes only:
  - `.bridge/`;
  - the planner's validated files;
  - its generated block in `CLAUDE.md`, the `AGENTS.md` symlink, and the `.bridge/` line
    in `.gitignore`;
  - the bookkeeping lines of ledger entries (6.5).

  It never commits, pushes or edits git config.
- **Every block of every message carries its origin:** `[owner]`, `[planner]`,
  `[supervisor]` or `[bridge]`. Nobody writes as the developer. `[bridge]` text is
  automated and never adds scope (L11, L12).
- `loop.log` records each message exactly as delivered, labels included.
  `events.jsonl` records the origin and recipient of every block.

## 3. Architecture

```
agent_bridge/
  cli.py          one function per command
  config.py       bridge.toml via tomllib, validated into frozen dataclasses
  statedir.py     .bridge/ layout, atomic JSON writes, lock, inbox, STOP
  journal.py      events.jsonl (structured), loop.log, review.log, console.log
  engine.py       the loop: a persisted state machine (section 7)
  planner.py      interview, drafting, re-planning, owner decisions, plan changes
  contract.py     contract hashes and drift, the generated CLAUDE.md block, ledger writes
  protocol.py     parse each role's output; compose labelled messages
  prompts.py      role texts and notes: generic text plus config and repo docs
  waits.py        WaitSpec and its conditions (UNTIL, FOR PID, FOR FILE)
  limits.py       reset-time parsing for every observed limit message
  repo.py         git: fingerprint, delta, new commits, trailer and push checks
  budget.py       caps and the pause summary
  owner.py        review (owner to-do) and the OWNER_REVIEW.md template
  live.py         renderers shared by foreground run, `logs -f` and `status`
  clock.py        Clock protocol (real, and fake for tests)
  templates/      bridge.toml, the CLAUDE.md block, OWNER_REVIEW.md (text templates)
  backends/
    base.py       Backend, Reply, Capabilities, errors
    opencode.py   OpencodeBackend and OpencodeServer
    claude_code.py
    fake.py
```

Single-threaded engine:
- Each agent turn runs a subprocess. A reader thread reads its stdout and streams events
  to the journal and the live view.
- For opencode, a monitor thread also polls the server (4.2).
- The engine waits on the turn with a deadline and a cancel flag. The flag is set by
  `stop --now`, a detected question, or a detected limit.

## 4. Engines

"Engine" is the config word; `Backend` is its interface in the code.

### 4.1 Interface

```python
@dataclass(frozen=True)
class Capabilities:
    read_only: str          # "enforced: --tools Read,Grep,Glob" | "read-only by instruction only"
    question_guard: str     # how an interactive question is prevented or caught
    live_attach: str | None # e.g. "opencode attach http://127.0.0.1:4096 --dir … --session …"

@dataclass
class Reply:
    text: str               # builder: all text parts in order; others: see 5.5
    session_id: str
    served_models: set[str]
    tool_calls: list[ToolCall]   # name, input summary (command, path), ok/error
    context_tokens: int | None   # input + cache read + cache write of the last request
    duration_s: float
    raw_path: Path          # .bridge/turns/<n>-<role>.jsonl, the raw event stream

class Backend(ABC):
    def version_check(self) -> str                    # raises Unsupported
    def health_check(self) -> Health                  # login, server; raises AuthFailed
    def capabilities(self) -> Capabilities
    def start_session(self, title: str) -> None       # next send() opens a new session
    def resume(self, session_id: str) -> None         # bind; checks the session belongs to this repo
    def send(self, message: str, *, timeout: float,
             on_event: Callable[[Event], None], cancel: threading.Event) -> Reply
    def abort(self) -> None                           # stop the in-flight turn for real
    @property
    def session_id(self) -> str | None
```

Normalized errors (all subclass `BackendError`):

| Error | Meaning | Carries |
|---|---|---|
| `RateLimited` | short throttling (429, overloaded) | `reset_at` or None |
| `SessionLimit` | plan usage window exhausted ("hit your session limit", pool exhausted) | `reset_at` or None |
| `AuthFailed` | 401, revoked or expired login | message naming the fix |
| `Timeout` | no completion within the role's timeout; the turn was aborted | partial text |
| `QuestionAsked` | the agent called an interactive question tool; it was rejected and aborted | the questions |
| `Unsupported` | wrong version, unknown flag, missing binary, billing mismatch | message naming the fix |
| `TransientError` | anything else (exit codes, network, empty reply) | detail |

A backend never retries; the engine owns every retry and sleep decision.

### 4.2 OpencodeBackend (1.18.x)

- **Version gate.** `opencode --version` must match `opencode.accept` (default `1.18`).
  - When a role uses opencode, anything else raises `Unsupported` before any model call.
    The message explains why 2.x is refused (no `--attach`/`--dir`, the omo plugin
    cannot load, Anthropic bills third-party apps from extra usage) and how to pin
    1.18.30.
  - When no role uses it, `init`, `run` and `check` still warn if the installed
    opencode is not 1.18.x, so switching a role to it later is not a surprise.
  - All three warn if `~/.config/opencode/opencode.jsonc` lacks `"autoupdate": false`.
  - Any "Unrecognized flag" in stderr is `Unsupported`, never transient (L6).
- **Server.**
  - One `opencode serve --hostname 127.0.0.1 --port <port>` per project, with cwd =
    repo, started detached (`start_new_session=True`) so it outlives the bridge.
  - Its output is appended to `serve.log` with a timestamped start marker.
  - If the port is already open, `GET /global/health` must answer as opencode with an
    accepted version. Otherwise the bridge stops with a message and touches nothing.
  - The bridge never stops a server.
- **Turn.** `opencode run --attach URL --dir REPO --format json --auto -m PROVIDER/MODEL
  [--variant V] (--session ID | --title "<project> <role> <date>") -- MESSAGE`.
  - Every message starts with a labelled line, so it can never begin with `-`. The `--`
    separator is used if the smoke test confirms 1.18.30 accepts it.
  - JSON events (`step_start`, `text`, `tool_use`, `step_finish`, `error`) are streamed.
  - The session id is persisted from the first event, not at the end.
- **New sessions are created by `opencode run`.** That gives them the `question`,
  `plan_enter` and `plan_exit` deny rules (L5). A session adopted with `pin` lacks those
  rules; `pin` and `status` say so.
- **Monitor thread,** every 15 s while a turn runs:
  - `GET /question?directory=REPO`: a question for this session or its children is
    rejected (`POST /question/{id}/reject`), the session is aborted, and `QuestionAsked`
    is raised.
  - `GET /session/status?directory=REPO`: a `retry` status whose `next` is more than 5
    minutes away, or whose message matches a limit pattern ("pool exhausted", "usage
    limit", "rate limit"), aborts the turn and raises `SessionLimit(reset_at=next)` or
    `RateLimited` (L2). Shorter retries are left to opencode.
  - New `serve.log` lines since the turn began are matched for limit and 401 messages
    ("next account free in 1h 45m", "Account usage window rejected … resets in N
    seconds"), as a second source for the same classification.
- **Abort for real.** On timeout, cancel, question or limit: `POST
  /session/{id}/abort?directory=REPO`, then end the client (SIGTERM, then SIGKILL after
  30 s). Before every send the session's status is read, and a busy session is aborted
  first, so retries never stack up as queued messages (L4).
- **Reply text.** Builder: every `text` part in order, which matches the old default
  output. Planner and supervisor: 5.5.
- **Served model.** The assistant messages created during the turn are read from
  `opencode.db` with `sqlite3` in `mode=ro` (URI) plus `PRAGMA query_only`. The session
  id is validated against `^ses_[A-Za-z0-9]+$` and passed as a bound parameter.
- **Context size.** From the `tokens` on the last `step_finish` event, with the DB as
  fallback.
- **Read-only: "read-only by instruction only".** The claude-bridge plugin overrides
  permission denies, so read-only cannot be enforced on this engine. It is backed by the
  repo delta and by the role's own `tool_use` events: any `write`, `edit`, `patch` or
  `bash` call by a planner or supervisor goes to `review.log`.
- **Agents.** Never passes `--agent` (omo drops custom agents and falls back to
  Sisyphus). The role travels in the message and the model is set with `-m`. Never
  `--pure` (401).
- **Unverified so far.** The routes (`/global/health`, `/session/status`, `/question`,
  `/question/{id}/reject`, `/session/{id}/abort`) and the `retry` status shape
  (`attempt`, `message`, `next`) come from the 1.18.30 binary. The smoke test checks
  their response shapes against a live server before the backend relies on them.

### 4.3 ClaudeCodeBackend

- **Turn.** The prompt is passed on stdin:

  ```
  claude -p --output-format stream-json --verbose --model MODEL [--effort V]
    (--session-id UUID | --resume UUID) --append-system-prompt ROLE
    --permission-prompts none -n "<project> <role>"
  ```

  - Builder: cwd = repo, `--permission-mode bypassPermissions` (the counterpart of
    opencode's `--auto`), `--disallowedTools AskUserQuestion`. With `git.push = "never"`,
    `--settings` adds deny rules for `git push`. The smoke test checks whether deny rules
    hold under `bypassPermissions`; if they do not, no-push stays an instruction backed by
    detection, and `check` says so.
  - Planner and supervisor: `--tools Read,Grep,Glob --allowedTools Read,Grep,Glob`. This
    is enforced.
  - Planner and supervisor cwd: a neutral per-role directory outside the repo
    (`~/.local/state/agent-bridge/<project-id>/<role>/`), with `--add-dir REPO`. That keeps
    the builder's `CLAUDE.md` out of their context, the same lesson as FillingQA's
    `claude_cli` generator. The smoke test confirms that reads through `--add-dir` work;
    if not, the fallback is cwd = repo, as in the old claude path.
- **Sessions.** The bridge generates the UUID and persists it before the first call,
  then uses `--resume UUID`. Session files live under `~/.claude/projects/`, keyed by
  cwd, so each role always runs from the same cwd.
- **Environment.**
  - `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed unless
    `billing.mode = "api-key"`.
  - `CLAUDECODE` and related variables are removed, so a bridge launched from a Claude
    Code terminal is not treated as nested.
  - The `system/init` event's `apiKeySource` is checked against the billing mode. A
    mismatch is `Unsupported`: it would bill the wrong account.
- **Errors.** A `result` with `is_error` is classified:
  - limit text ("You've hit your session limit · resets 9:40pm (Asia/Calcutta)", "usage
    limit reached", weekly variants) is `SessionLimit`, with the reset parsed by
    `limits.py`;
  - 401, "invalid api key", "please run /login", "OAuth token expired" or "Failed to
    authenticate" is `AuthFailed`;
  - 429 or overloaded is `RateLimited`;
  - anything else is `TransientError`.

  A timeout kills the process group. There is no server-side turn.
- **Health.** `claude --version`, and `claude auth status` (JSON: `loggedIn`,
  `authMethod`, `subscriptionType`), which makes no model call. It is only a pre-check:
  - on 2026-10-06 it reported `loggedIn: true` while the login was already dead;
  - the first call then failed with `is_error: true`, `subtype: "success"`,
    `terminal_reason: "api_error"` and "Failed to authenticate: OAuth session expired
    and could not be refreshed";
  - after that call, `auth status` reported `loggedIn: false`.

  So `AuthFailed` is always taken from turn results, never assumed from the pre-check.
- **Served model.** `message.model` on each assistant event. `modelUsage` is not used,
  because Claude Code also calls small models for housekeeping.
- **No SDK.** The Claude Agent SDK drives this same CLI. Calling the CLI directly keeps
  the tool dependency-free.

### 4.4 FakeBackend

- Scripted from a list of steps or a callable. A step is either a reply (text, served
  model, tool calls, context tokens, optional file changes applied to the temporary
  repo) or an error to raise, optionally after advancing the fake clock.
- It records every message it was sent, so tests can assert on labels, notes and order.
- It is the only engine the test suite calls.
- Subprocess plumbing (timeouts, aborts, streaming) is tested separately, with fake
  `opencode` and `claude` executables and a stdlib `http.server` standing in for the
  opencode server.

## 5. Message protocol

### 5.1 Message layout

Builder message (empty blocks omitted):

```
[bridge] Headless run: never use a question or ask-user tool; put questions under
DECISIONS NEEDED. To pause for a job or a time, end your report with one line:
WAIT FOR PID <n> | WAIT FOR FILE <path> | WAIT UNTIL <ISO-8601 time>.
[bridge] The repository is <absolute path>. Use absolute paths under it for every file, and
start every shell command with `cd <repo> &&`. Never work in any other repository. (L22)
[bridge] Blocked until the owner settles them: R-4, Phase 3 (PC-3).   (only while any)
[owner] (verbatim; binding)
<owner text>
[planner] <the kickoff, or a plan change: summary, DEC id, diff path>
[bridge] <one note: resume after timeout | question rejected | wait over | cut off by a
usage limit | fresh-session handoff | contract file changed>
[supervisor]
<the supervisor's REPLY>
```

Supervisor message, each turn:

```
[bridge] You are the read-only SUPERVISOR of <project> (<how read-only is enforced>).
The contract is docs/PRD.md and CLAUDE.md, approved <date> (<short hashes>). Absolute
paths under <repo> only. Output VERDICT, optional directive lines, REPLY.
[bridge] Re-verify the builder's claims against the repo this turn. Your memory of
earlier turns is context, not evidence.
[bridge] Since your last turn: HEAD a1b2c3d..e4f5a6b (3 commits: <subjects>); working
tree: 2 modified, 1 untracked. Builder turn: 41m, ended normally.
[bridge] The builder asked for: WAIT FOR PID 64410. It is honoured after your reply
unless you write NO WAIT or a different WAIT line.
[bridge] The builder listed 2 DECISIONS NEEDED; answer each in your REPLY.
[bridge] Blocked: R-4, Phase 3 (waiting on PC-3); Phase 5 (paused at the re-plan cap,
OPEN-012). Your SCOPE must avoid them.
[bridge] The builder says Phase 2 is complete. If you verify its exit criteria, write
PHASE COMPLETE: Phase 2.
[owner] / [planner] (verbatim) every block the builder received from someone other than
you since your last turn, so you always know what it was told (L11)
===== BUILDER REPORT =====
<the last 24,000 chars; the full text is in the turn file>
===== END =====
```

Planner message:

```
[bridge] You are the PLANNER of <project> … (the role, in a session's first message)
[owner] <the idea | the interview answers | the decisions doc>
[supervisor] REPLAN: <problem>, with its VERDICT and the builder report it was reviewing
[bridge] <contract hashes, current phase, waiting plan changes, re-plans so far this phase>
```

The first message of every session also carries the role text (8.3).

### 5.2 Supervisor output grammar

```
[optional preamble]
PROJECT COMPLETE                      optional (see below)
VERDICT: <one line>
SCOPE: <requirements and phases the REPLY's work belongs to, e.g. R-3, R-5, Phase 2> | none
PHASE COMPLETE: <phase>               optional; only after verifying its exit criteria
WAIT UNTIL <ISO-8601>        [MAX <duration>]       optional directives, one per line
WAIT FOR PID <n>             [MAX <duration>]
WAIT FOR FILE <path>         [MAX <duration>]
NO WAIT
ROTATE BUILDER
REPLAN (<phase>): <what in the plan is wrong or blocked, with evidence>
ESCALATE: <question>                  honoured only in mode = "escalate"
REPLY:
<message for the builder>
```

- The output is split at the first `REPLY:` line. Directives are read only from the
  part above it, so a WAIT quoted in the reply body does nothing.
- Lines are normalized before matching (markdown `*_#>\`` and a leading `- ` stripped).
- **Sentinel.** `PROJECT COMPLETE` counts as a standalone line above `REPLY:` (a
  preamble is tolerated), or as the first non-empty line of the body. Deeper in the body
  it is ignored, with a warning (C4).
- **`SCOPE` is required on every reply.** `none` means the reply directs no new work
  (verification, acknowledgement, a wait). The bridge checks it against the blocked set
  (6.6):
  - a missing SCOPE gets one nudge, then the reply is sent with a warning;
  - a SCOPE that touches a blocked requirement or phase gets one nudge. A second one
    holds the reply unsent and pauses the run, because directing work into blocked areas
    breaks the contract;
  - a reply whose body only mentions a blocked id is sent, with a `review.log` note,
    since a mention can be a warning to stay away.
- **`PHASE COMPLETE`** marks a verified phase boundary. It is recorded in the state and
  the ledger, and it triggers rotation (7.3).
- **`REPLAN`** sends the problem to the planner (6.6). The phase in parentheses defaults
  to the current phase. The REPLY is held, and the supervisor writes a new one after the
  planner answers.
- **No `REPLY:`.** One nudge in the same session. If the output is still malformed, the
  whole text is sent as the reply and a warning is logged (C5).
- **Empty output.** One nudge in the same session, then `TransientError`.
- Durations: `90s`, `30m`, `2h`, `1d`, `1h30m`; a bare number means seconds.

### 5.3 Planner output grammar

One of four forms:

```
QUESTIONS:
1. <question>
   Recommended: <answer>
   Why it matters: <what changes with the answer>
2. …
```

```
PLAN:
SUMMARY: <one paragraph>
=== FILE docs/PRD.md ===
<full content>
=== END FILE ===
=== FILE CLAUDE.md ===
…
=== END FILE ===
(and docs/DECISIONS.md, docs/OPEN.md, bridge.toml)
KICKOFF:
<the builder's first instruction>
```

```
CHANGE: <title>
REASON: <what is wrong or blocked, with evidence and paths>
MATERIAL: yes | no
AFFECTS: <requirements and phases the change touches, e.g. R-4, Phase 3>
=== EDIT docs/PRD.md ===
--- FIND ---
<exact current text; must occur exactly once>
--- REPLACE ---
<new text>
=== END EDIT ===
LEDGER:
<context, options, decision and reasons for the ledger entry>
TO SUPERVISOR:
<guidance>
KICKOFF:                              only when answering `decide`
<the builder's instruction>
```

```
NO CHANGE: <reason>
TO SUPERVISOR:
<guidance>
```

Edits are find/replace blocks, not unified diffs:
- the bridge checks that each FIND matches exactly once, applies the edit, and renders
  the unified diff itself (`.bridge/plan/changes/PC-NNN.diff`);
- the diff the owner reviews is therefore exact, whereas model-written diffs often carry
  wrong hunk headers;
- a FIND that does not match goes back to the planner once.

### 5.4 Builder report signals

- `WAIT …` lines anywhere in the report are a wait request (the last one wins). The
  supervisor sees it, and it is honoured unless the supervisor writes `NO WAIT` or its
  own WAIT. The builder knows the pid or reset time; the supervisor keeps the veto.
- The `DECISIONS NEEDED:` section is extracted, highlighted to the supervisor, and shown
  in `status`.
- A phase claim matching `rotation.phase_complete_pattern` is passed to the supervisor
  only as a hint. Only the supervisor's `PHASE COMPLETE` directive is a boundary.
  Boundaries are recorded once and never re-derived on a retry (C1).
- Changes to contract files are detected from git, not from the text (6.8).

### 5.5 Planner and supervisor reply text

The text of the last step that contains the form's marker (`REPLY:`, or `QUESTIONS:`,
`PLAN:`, `CHANGE:`, `NO CHANGE:`). If no step contains one, all text parts are joined.
That keeps the old "last step" behaviour and survives a VERDICT written in an earlier
step.

## 6. The planner

### 6.1 `agent-bridge new "<idea>"`

1. **Repo.**
   - `--repo PATH`, default the current directory. A missing directory is created and
     `git init`-ed.
   - `new` refuses if the contract files already exist. Existing files are adopted with
     `init` and `approve` (6.4), or changed with `decide` (6.7).
2. **Engines.** The three roles' engines and models come from `--planner`, `--supervisor`
   and `--builder` (`ENGINE:MODEL`), or the defaults (section 9). They are recorded in
   `.bridge/state.json`, so the planner can start before `bridge.toml` exists.
3. **Interview.**
   - The planner reads the repo (read-only) and returns one batch of numbered questions,
     at most 8, each with a recommended answer and what changes with it. It always asks
     first: a PLAN at this stage is refused (DEC-010).
   - It may ask one follow-up batch, only when an answer leaves a choice it cannot
     settle conservatively.
4. **Answers.**
   - In a terminal, `new` prints the questions and reads the owner's reply inline. "use
     your recommendations", or an empty reply, takes every recommendation. A partial
     reply ("2: Postgres; the rest as recommended") is passed on verbatim.
   - Without a terminal, `new` stops in `INTERVIEW` and prints the questions.
     `agent-bridge say "<answers>"` delivers them and runs the drafting turn in the
     foreground.
   - With `--auto-approve`, the bridge tells the planner, labelled `[bridge]`, that the
     owner chose `--auto-approve` and it is to use its recommendations.
5. **Drafting.** The planner returns the five contract files and a kickoff (5.3). The
   bridge then:
   - validates them (6.2);
   - writes them uncommitted, so the owner can read and edit them in place;
   - adds its block to `CLAUDE.md` (6.3), creates the `AGENTS.md` symlink, and adds
     `.bridge/` to `.gitignore`;
   - stops in `PLAN_REVIEW`, with a summary and the next command.

### 6.2 The contract files

| File | Content | The bridge checks |
|---|---|---|
| `docs/PRD.md` | goals, non-goals, numbered requirements (R-1…), phases in order; each phase has exit criteria as checkable bullets, and its owner-only items; a Results section saying what counts as a real result and what is a development run | Goals, Non-goals, Requirements and Results headings; at least one phase; an exit-criteria list under every phase |
| `CLAUDE.md` | project rules, including the honesty rules: no fabricated numbers, no tuning thresholds to pass, dev runs labelled as such, failures reported as failures | non-empty; the bridge adds its own block (6.3) |
| `docs/DECISIONS.md` | the decision ledger, seeded with the planner's design choices and the interview answers | entries in the ledger format (6.5) |
| `docs/OPEN.md` | open questions and OWNER-BLOCKED items known up front | every entry has a status: OPEN, OWNER-BLOCKED or RESOLVED |
| `bridge.toml` | roles, engines, models, budgets; optional in the PLAN, since leaving it out keeps the valid provisional file (DEC-011) | parses with `tomllib` and validates (section 9); owner-only settings below |

- Any other path is refused. Paths come from `[project]`, so a migrated project keeps
  `docs/TRADEOFFS.md` as its ledger.
- **Results register.** `docs/RESULTS.md` is not a contract file.
  - The builder creates it with its first result, and keeps one row per reported number:
    id, what was measured, value, kind, run id or artifact path, and commit.
  - Kind is `development` or `real`, as the PRD's Results section defines them.
  - The supervisor checks each row against its artifact.
  - The register feeds the report (6.10).
- A failed check goes back to the planner once, with the errors. A second failure pauses,
  with the errors in `PAUSED.md`.
- **Owner-only settings.** `git.push = "allowed"` and `billing.mode = "api-key"` come
  only from the owner. Under `--auto-approve` the bridge rejects them in the planner's
  `bridge.toml`; otherwise it lists them for the owner's attention at approval. Engines
  and models given to `new` as flags are kept as given.

### 6.3 The generated `CLAUDE.md` block

Between `<!-- agent-bridge:begin (generated; edits are overwritten) -->` and
`<!-- agent-bridge:end -->`, the bridge maintains the rules every project needs,
whatever its plan:
- the message labels, and what each origin may instruct;
- headless: never use a question tool; questions go under `DECISIONS NEEDED:`;
- the end-of-turn report (`DECISIONS NEEDED:`, `PROCEEDING:`) and the WAIT lines;
- long jobs run as background processes, with their command, pid and log recorded;
- the builder never edits the contract files; changes are proposed under
  `DECISIONS NEEDED:`;
- requirements and phases listed as blocked (by a waiting plan change or a paused phase)
  are off limits until the owner settles them;
- every reported number gets a row in the results register, labelled `development` or
  `real`;
- commits use the repo's local author, carry no Co-Authored-By or AI attribution, are
  never force-pushed, and are pushed only if `git.push = "allowed"`;
- the baseline honesty rules.

The block is versioned. The bridge refreshes it when its version changes and never
touches text outside the markers. On a migrated project the block is added only by
`init --write-rules`, after showing the diff.

### 6.4 Approval gate

- `run` refuses to start until a contract is approved. Nothing builds before that.
- **`agent-bridge approve`** in `PLAN_REVIEW`:
  1. hashes `PRD.md`, `CLAUDE.md` and `bridge.toml` as they are now, owner edits included;
  2. records the approval in the state, and appends a ledger entry: "Plan approved by the
     owner (agent-bridge approve, <time>; PRD sha256 …)";
  3. changes the Status of the seeded entries from `PROPOSED` to `APPROVED`;
  4. starts the loop: `--forever` by default, or `--loop N`, `--background`, `--no-run`.
     The first builder message is the planner's kickoff, which begins with committing the
     contract files by explicit path.
- **`--auto-approve`** skips the interview wait and the review. Every planner choice
  (each seeded ledger entry, and each recommended answer it used) gets the Status
  `AUTONOMOUS DECISION - owner to review`, and the loop starts.
- **Adopting an existing contract.** A project that already has a PRD and rules (the
  three migrated projects) skips the planner. `init` writes `bridge.toml`, and `approve`
  adopts the current files as the approved contract. The PRD structure checks are
  warnings there, not errors, but the PRD and the rules must exist.
- **Unfinished planning.** `approve` refuses while the planner has not finished (an open
  interview, or a draft that failed the checks). `agent-bridge say` answers the planner in
  both cases (DEC-012).

### 6.5 Ledger

```
## DEC-012 Split phase 3 into 3a and 3b
- Decided by: planner (re-plan requested by the supervisor at exchange 41)
- Status: AUTONOMOUS DECISION - owner to review
- Change: PC-003 (.bridge/plan/changes/PC-003.diff)

Context, options, decision and reasons, written by the decider.
```

- For every entry it records, the bridge assigns the DEC- and PC- numbers and writes the
  heading and the "Decided by", "Status" and "Change" lines. The planner writes only the
  body, so it cannot claim the owner's approval.
- Status values:
  - `PROPOSED`
  - `APPROVED by the owner <date>`
  - `AUTONOMOUS DECISION - owner to review`
  - `OWNER DECISION (<source>)`
  - `APPLIED (non-material)`
  - `AWAITING OWNER`
  - `REJECTED by the owner: <reason>`
  - `SUPERSEDED by DEC-n`
  - `VERIFIED (phase boundary)`, for the entry the bridge records at the supervisor's
    `PHASE COMPLETE` (DEC-007)
- The builder records its own in-loop decisions (approved by the supervisor) in the same
  format, with "Decided by: builder, approved by the supervisor", as the `CLAUDE.md`
  block requires.
- `review` lists every `AUTONOMOUS DECISION - owner to review` and `AWAITING OWNER`
  entry.

### 6.6 Re-planning

- **Trigger.** Only the supervisor, with `REPLAN: <problem>`, when the plan itself is
  wrong or blocked: a spec conflict, a phase that cannot meet its exit criteria, or a
  requirement contradicted by real data. It never improvises around the plan. The
  builder raises such problems under `DECISIONS NEEDED:`; the owner uses `decide`.
- **Proposal.** The planner investigates (read-only) and answers either:
  - `CHANGE`: edits, a reason, materiality, the ledger body, and a note for the
    supervisor; or
  - `NO CHANGE`: a reason and a note.
- **Material or not.** The planner classifies, and the bridge can only raise the
  classification. An edit is material when it touches goals, non-goals, a requirement, a
  phase's scope or order, an exit criterion, the owner-only items, or any number.
- **Never loosened without the owner.** A proposal that weakens an exit criterion or a
  threshold is never applied automatically, even under `--auto-approve`. It is recorded
  as `AWAITING OWNER`, with an OWNER-BLOCKED item in `OPEN.md`, and the phase can end at
  the owner-blocked boundary. A low result is a finding, not a reason to move the bar.
- **Outcomes:**

  | Proposal | `--auto-approve` off | `--auto-approve` on |
  |---|---|---|
  | non-material | applied; `APPLIED (non-material)` | the same |
  | material | not applied; `AWAITING OWNER` and an OWNER-BLOCKED item; the loop continues on the current contract | applied; `AUTONOMOUS DECISION - owner to review` |
  | weakens a criterion or threshold | `AWAITING OWNER` | `AWAITING OWNER` |
  | NO CHANGE | the planner's note goes to the supervisor | the same |

- After any outcome, the supervisor gets a turn with the planner's answer (labelled
  `[planner]`) and the same builder report. It writes its instruction under the contract
  as it now stands.
- `agent-bridge approve PC-n` applies a waiting change, at the next step boundary if a
  loop is running. `--reject PC-n --reason "…"` records `REJECTED by the owner` and tells
  the planner and the supervisor.
- **What a waiting change blocks.** Every plan change lists what it affects: the
  planner's `AFFECTS` line, plus the requirements and phases enclosing each edit, which
  the bridge finds in the PRD itself. While a change is `AWAITING OWNER`, its affected
  requirements and phases are in the blocked set:
  - the supervisor is told every turn, and its `SCOPE` is checked against them (5.2);
  - the builder is told every turn (5.1);
  - `status` and the report list them.

  The set clears when the owner approves or rejects the change.
- **The re-plan cap pauses the phase.** `budget.max_replans_per_phase` (default 3) counts
  REPLANs per phase. The one that would exceed it is not sent to the planner. Instead the
  bridge:
  - writes an OWNER-BLOCKED item to `OPEN.md` ("Recorded by: agent-bridge"), with the
    phase's re-plan history and the open problem;
  - marks the phase paused, which puts it in the blocked set;
  - answers any further REPLAN for that phase with a `[bridge]` refusal, not a planner
    call.

  Work continues only on what the contract allows outside that phase, or the project ends
  at the owner-blocked boundary. `decide`, or settling the phase's waiting changes, lifts
  the pause and resets the count.

### 6.7 Owner decisions: `agent-bridge decide DOC`

This is the OWNER_REVIEW.md flow, routed through the planner:
1. The bridge checks that DOC has D-items (`## D1 …`) and a "Done when" checklist.
2. The planner reads DOC and the contract. It returns `CHANGE` blocks that put the
   owner's decisions into the PRD, `OPEN.md` and the ledger (status `OWNER DECISION (D3,
   docs/OWNER_REVIEW.md)`), plus a kickoff.
   - Owner decisions need no further approval.
   - Where a decision is ambiguous or conflicts with the PRD, the planner first asks
     numbered questions with recommendations, answered as in 6.1.
3. The bridge applies the edits, starts a fresh supervisor (L15), and sends the planner's
   kickoff: commit DOC and the plan changes first, work through D1..Dn in order, report
   hashes and run ids, done when the checklist holds.
4. It then runs `--forever`, or `--loop N`, `--background` or `--no-run`.

If a loop is already running, `decide` queues DOC, and it is handled at the next step
boundary. Owner decisions supersede a builder report that was waiting for review, and an
active wait; both are logged as events.

### 6.8 Contract integrity

- At approval, and after each applied change, the bridge records the sha256 of the PRD,
  the rules and `bridge.toml`.
- It compares them before and after every builder turn (DEC-006). A change made during
  the turn goes to `review.log`, and to the supervisor as `[bridge] The builder changed
  docs/PRD.md during its turn (diff …). The contract changes only through the planner.`
  The bridge never reverts anything itself.
- A change made outside any turn (the owner editing a file between runs) is reported at
  the next `run` as `CONTRACT CHANGED OUTSIDE A TURN`, not blamed on the builder. The
  owner re-approves the current files with `agent-bridge approve`.
- A running loop never reloads `bridge.toml`. A change shows in `status` and `check`, and
  applies at the next `run`.

### 6.9 Planner sessions

- The planner keeps one persistent session per project, so it remembers why the plan
  looks the way it does.
- It rotates on `planner_max_context_tokens` or `pin --new-planner`. The plan is written
  down, so a fresh session re-reads the contract and the ledger.
- Default: Claude Code with `claude-fable-5-1` at `max` effort, the strongest model
  available to the CLI when this was written.

### 6.10 Owner-review report: `agent-bridge report`

Generated automatically at PROJECT COMPLETE, and on demand with `agent-bridge report`. It
is written to `.bridge/reports/<time>.md`, with `.bridge/report.md` pointing at the
latest. Every item links to its ledger entry or file line, and to the commit that
introduced it.

1. **Summary.** Contract approval, the phases completed (with the exchange that verified
   each), exchanges, wall time, engines and models per role, read-only enforcement, and
   the warning count.
2. **Decisions to review.** Every `AUTONOMOUS DECISION - owner to review` ledger entry:
   who decided it, the decision in one line, `docs/DECISIONS.md:<line>`, and the commit.
3. **OWNER-BLOCKED items.** From `OPEN.md`: what is needed from the owner, the file line
   and the commit. This includes the items the bridge recorded itself (the re-plan cap).
4. **Plan changes.** Every PC-n: its status (applied, awaiting the owner, rejected), what
   it affects, the diff, the ledger entry, and the commit that carried it.
5. **Results: development vs real.** The results register split by kind, each row with its
   run id or artifact, and its commit. Development results are never presented as real
   ones. A missing or empty register is stated, not skipped.
6. **Warnings.** Everything in `review.log` since the contract was approved, grouped:
   tripwire, served-model mismatch, contract drift, danger commands, commit trailers.

How links are built:
- Links are relative file links with line numbers.
- Commits are short hashes, plus a URL when the repo has a GitHub remote.
- The bridge finds each commit with `git log` on the line or the blob. Nothing in the
  report is taken from an agent's prose.

`review` (the owner to-do) is sections 2–4 of the report, in checklist form.

## 7. Loop state machine

The state lives in `.bridge/state.json`, written atomically (temp file plus rename)
before each action, so a crash or restart resumes exactly where it stopped.

```
Planning
  new ──► INTERVIEW ──answers──► DRAFTING ──► PLAN_REVIEW ──approve──► BUILDER_TURN
  (INTERVIEW and PLAN_REVIEW wait for the owner; the process exits unless a terminal is attached)

Loop
  BUILDER_TURN ───report───────────────► SUPERVISOR_TURN
  SUPERVISOR_TURN ──REPLY──────────────► BUILDER_TURN
                  ──PHASE COMPLETE + REPLY──► BUILDER_TURN, in a fresh builder session (7.3)
                  ──WAIT ──────────────► WAITING ──met / MAX / owner message──► BUILDER_TURN
                  ──REPLAN─────────────► REPLANNING ──planner answer──► SUPERVISOR_TURN
                  ──REPLAN, phase at the cap──► SUPERVISOR_TURN, with a [bridge] refusal (6.6)
                  ──PROJECT COMPLETE───► COMPLETE (report written, 6.10)
                  ──ESCALATE (escalate mode)──► PAUSED(escalation)
  decide ──► REPLANNING (owner decisions) ──► BUILDER_TURN (planner kickoff, fresh supervisor)
  COMPLETE ──say / decide / run --kickoff──► new work, always with a fresh supervisor

Any agent turn
  SessionLimit / RateLimited ──► SLEEPING (until the reset + 2 min) ──► the same turn
  transient error ─────────────► SLEEPING (backoff) ─────────────────► the same turn
  AuthFailed ──────────────────► PAUSED(auth)
  Unsupported ─────────────────► exit 2 (never loops)
Any step boundary
  STOP ────────────────────────► PAUSED(stop), pending message saved
  budget cap ──────────────────► PAUSED(budget), summary written
```

| State | Persisted fields | Leaves when |
|---|---|---|
| `INTERVIEW` | the questions, the batch number | the owner answers (inline or `say`) |
| `DRAFTING` | the answers, the attempt | the files validate, or a second failure pauses |
| `PLAN_REVIEW` | the drafted file hashes | `approve` |
| `IDLE` | — | a kickoff or `say` arrives |
| `BUILDER_TURN` | the pending message blocks, attempt, `in_flight` | report (to `SUPERVISOR_TURN`) or an error (8.1) |
| `SUPERVISOR_TURN` | the path of the report under review, attempt | parsed output |
| `REPLANNING` | the problem or decisions doc, the source, attempt | the planner's answer is applied (6.6, 6.7) |
| `WAITING` | WaitSpec, start, deadline, pending reply, who asked | condition met, MAX reached, owner message, STOP |
| `SLEEPING` | until, reason, the state to resume | time reached, STOP |
| `PAUSED` | reason, detail, the state to resume | `agent-bridge run` again (the process has exited) |
| `COMPLETE` | time, report path | new owner work, which always gets a fresh supervisor (L15) |

**PAUSED means the process exits** with code 3, after writing `.bridge/PAUSED.md`:
- the reason and the counters;
- the last verdict and the open DECISIONS NEEDED;
- where the pending message is;
- the exact command to continue.

Nothing runs while paused, and `agent-bridge run` resumes from the saved state.

**Crash recovery.** A state saved with `in_flight` set means the bridge died mid-turn. On
restart the session is checked; if busy, it is aborted, and the message is resent with
"the bridge restarted while you were working; check git status before continuing".

### 7.1 WAIT

- **Who.** The supervisor, as a directive. The builder, as a request in its report,
  honoured unless the supervisor writes `NO WAIT` or its own WAIT.
- **Conditions,** checked every 15 s with no model calls:

  | Form | Satisfied when | Notes |
  |---|---|---|
  | `WAIT UNTIL <time>` | the time is reached | a time with no offset is local; a time in the past is satisfied at once |
  | `WAIT FOR PID <n>` | the process is gone | the start time (`ps -o lstart=`) is recorded, so a reused pid counts as gone; a pid not running at the start is satisfied at once |
  | `WAIT FOR FILE <path>` | the path exists | a relative path is resolved against the repo |

- **Bounds.** `MAX` defaults to `waits.max` (24 h). Longer requests are clamped, and the
  clamp is logged. A multi-day wait, for a weekly quota say, therefore costs at most one
  exchange a day.
- **Wake-up.** The next builder message gets `[bridge] The wait (WAIT FOR PID 64410) is
  over: the process exited at 03:31. Check the result before anything else.` (or "MAX
  reached without the condition"), followed by the supervisor's REPLY from the turn that
  set the wait. There is no extra supervisor call.
- **Interruptions.** STOP ends a wait at once and saves the pending reply. An owner `say`
  ends it, and is delivered with a note that the condition was not met.
- **Backstop for supervisors that do not use WAIT (L1):**
  - after 3 consecutive exchanges with no repo change and no active wait, the supervisor
    gets a `[bridge]` note: if the builder is waiting, use WAIT instead of acknowledging;
  - after 5, the bridge itself sleeps (30 min doubling to 2 h, X's backstop) and logs it;
  - after `budget.max_unchanged_exchanges` (default 12), the run pauses.

### 7.2 Usage limits

- A `SessionLimit` with a known reset sleeps until the reset plus 2 minutes, with no
  model calls, then retries the same turn.
- If a builder turn was cut off, the resend says so: "you were cut off by a usage limit
  at 05:01; check git status and the log, then continue".
- An unknown reset backs off 15 min, doubling to 1 h, one request per attempt.
- 48 hours of consecutive limit sleeping pauses the run.
- Sleeps show in `status` ("sleeping until 17:30, session limit, claude-code/builder").

### 7.3 Rotation (L19, L15)

- **Builder.** A new session opens with the next builder message when any of these
  happens:
  - the supervisor writes `PHASE COMPLETE`, a verified phase boundary (on by default:
    `builder_on_phase_complete = true`);
  - the last request's context reaches `rotation.builder_max_context_tokens` (default
    600k);
  - the supervisor writes `ROTATE BUILDER`.

  The supervisor is told one turn ahead, so it can make its REPLY self-contained. The new
  session gets a `[bridge]` handoff listing, from config:
  - the rules and the PRD;
  - the worklog, read from its end back to the current phase;
  - `OPEN.md` and the ledger;
  - `git log --oneline -20`, `git status`, and any background jobs the worklog records.

  The old session is retired in `sessions.json`, never deleted.
- **Supervisor.** Rotated at a verified phase boundary (on by default; the three copies
  rotated on the builder's claim instead), at
  `supervisor_max_context_tokens`, on `--new-supervisor`, and always before new work
  after `COMPLETE`. A fresh supervisor's first message includes the last verdict and "a
  previous supervisor session reviewed exchanges 1–N; re-verify from the repo".
- **Planner.** 6.9.
- A timeout never rotates a session (L3).

## 8. Errors, safety and prompts

### 8.1 Errors

| Error | Builder turn | Planner or supervisor turn |
|---|---|---|
| `Timeout` | aborted server-side; resent with a resume note; 3 in a row pauses (L14) | aborted; retried in the same session after backoff; 3 in a row pauses |
| `QuestionAsked` | rejected and aborted; resent listing the questions and "answer them under DECISIONS NEEDED"; 3 in a row pauses | the same, with a note |
| `SessionLimit`, or `RateLimited` with a reset | sleep until the reset (7.2) | the same |
| `RateLimited`, no reset | backoff 60 s doubling to 30 min | the same |
| `AuthFailed` | pause: "Run `claude login` (or re-enroll the pool account), then `agent-bridge run`" (L18) | the same |
| `Unsupported` | exit 2 with the fix | the same |
| `TransientError` | backoff 60 s doubling to 30 min; 10 in a row pauses | the same |
| Empty reply | `TransientError` | one in-session nudge, then `TransientError` |

Every retry, sleep and pause is an event in `events.jsonl` and a line on the console.
Pauses, and anything the owner should review, also go to `review.log`.

### 8.2 Safety

- **One bridge per project.** An `fcntl.flock` on `.bridge/lock` records pid, start time
  and argv. The OS releases it if the process dies (C2).
- **STOP.**
  - `agent-bridge stop` writes `.bridge/STOP`, which is checked at every step boundary
    and every 5 s during sleeps and waits.
  - The running turn finishes, and the pending message is saved to `unsent_reply.md` on
    every path (L13).
  - `stop --now` also aborts the running turn, and saves the message with a resume note.
  - A new `run` clears STOP only after taking the lock.
- **Read-only, reported per role** (section 2). The tripwire logs which paths changed
  during a planner or supervisor turn, not just that something changed (L10).
- **Contract integrity** (6.8).
- **Tool-call audit, from events.** The builder's shell commands are matched against
  danger patterns, and hits go to `review.log` with the exact command. Prose is not
  matched (L9). The patterns: force push; `push` when `git.push = "never"`; `reset
  --hard`; `rm -rf` outside the repo; `DROP TABLE`; plus `safety.danger_commands`.
- **The builder stays in the repo (L22).** After each builder turn, its tool calls are
  checked for paths under `/Users/` (or the owner's home) outside the repo:
  - a write there (a write or edit tool, or a shell command that commits, adds, moves,
    removes or redirects) pauses the run, so the owner can check the other place first
    (DEC-013);
  - a read there is only reported.
- **Git checks** after each builder turn:
  - new commits whose message carries `Co-Authored-By` or AI attribution are flagged;
  - so are commits whose author differs from the repo's local `user.email`;
  - so are remote-tracking refs that moved while `git.push = "never"`.
- **Sessions bound to the repo.** `pin` and startup check that an opencode session's
  directory (a read-only DB lookup), or a claude session file's cwd, is this repo or the
  role's neutral directory (L17). There is no `--continue` fallback (C3).
- **Environment.**
  - `ANTHROPIC_API_KEY` is stripped for every child unless `billing.mode = "api-key"`.
  - `project.env_file` (opt-in) loads variables into the builder's environment only.
    For an opencode builder that means the server's environment, so the server must be
    restarted for a change to apply; `status` warns when the file has changed since the
    server started.
- **caffeinate.** On macOS, `run` starts `caffeinate -is -w <own pid>`, which exits with
  the bridge.

### 8.3 Prompts

- **Generic text is built in:**
  - the shared parts of the three copies' `SYSTEM` and `AUTONOMOUS`;
  - the read-only and absolute-path rules, and re-verify;
  - the output grammars, the autonomous rules, and the completion rule.
- **The supervisor holds the builder to the contract and never invents scope.** Every
  instruction traces to the PRD (it cites the requirement or the phase's exit criteria).
  Work outside the PRD is refused, and a gap in the plan is a `REPLAN`, not an
  improvisation.
- **Completion** is reached when every phase's exit criteria are met, or the phase is
  blocked only on OWNER-BLOCKED items or plan changes awaiting the owner, and the
  supervisor has checked this in the repo. When every `Phase N` heading of the PRD has a
  verified PHASE COMPLETE, the bridge says so to the supervisor as a fact (DEC-014).
- **Project text comes from the contract files.** For a migrated project it also comes
  from `project.supervisor_rules` (for example `docs/SUPERVISOR.md`, holding the project
  principles F, X and N hard-coded), inlined into the supervisor's first message.
- **Repeat detection** no longer injects task text into the builder. It feeds the idle
  backstop (7.1), and its notes go to the supervisor, labelled `[bridge]` (L12).

### 8.4 Hardening after the 2026-10-06 audit

An outside audit reproduced each finding below with a script; each fix has a test in `tests/test_audit.py`
(DEC-020).

- **The agent never outlives its turn.**
  - Any exception while a turn runs ends the agent's whole process group: Ctrl-C, a monitor stop, or a
    parser error.
  - SIGTERM and SIGHUP raise the same interrupt as Ctrl-C in every command that takes the lock, so a
    closed terminal ends cleanly.
  - On opencode the turn is also aborted on the server.
  - The running agent's pid and start time go to `.bridge/agent.pid`. A bridge killed with SIGKILL cannot
    clean up, so the next start ends that process, if it is the same one (same start time), before the
    turn is resent.
- **Prevent, not only detect.** On macOS the Claude Code builder runs under `sandbox-exec`.
  - Its whole process tree may write only inside the repo, the temp folders, and the tools' own state:
    Claude Code's files and caches, plus `safety.sandbox_writable`.
  - It was verified live with Haiku: a write inside the repo worked, and a write to the home folder got
    "operation not permitted".
  - Detection still runs behind it. It now follows `~`, `$HOME`, `cd` and `git -C`. It masks the repo's
    own path, which may contain spaces. When the builder wrote or committed but the repo did not change
    (the L22 signature), the supervisor gets a note.
  - The opencode builder is not sandboxed (OPEN-004).
- **What a plan change may touch.**
  - A re-plan that edits `bridge.toml` is always material, and is validated like a full plan.
  - It may never change the owner-only settings: `git.push`, `billing.mode`, `project.verify`, the
    sandbox settings and `notify.command`.
  - Existing `Status:` and `Decided by:` lines in the ledger and open items cannot be changed or removed.
    Open items are the exception during the owner's own `decide`.
  - No new line may claim the owner's decision.
- **One bad input never stops everything.**
  - A queued owner item that fails is moved to `.bridge/inbox/failed/` with its error.
  - Owner text is never used as a regex template.
  - Display and contract reads tolerate bad UTF-8.
  - The UI catches refresh errors, a malformed `$EDITOR` and an unknown locale.
- **Near-misses are loud.** The parsers stay strict, but `PROJECT COMPLETE.` or `PROJECT COMPLETE: …`
  gets one nudge instead of being ignored. Other fixes:
  - a SCOPE written as a list is read;
  - an empty SCOPE is a near-miss;
  - `DECISIONS NEEDED: none` ends the list;
  - a note after `WAIT FOR FILE <path>` is not part of the path;
  - "401" means a login failure only next to an auth word or an HTTP status;
  - billing errors pause the run instead of retrying.
- **Evidence for the supervisor.**
  - `project.verify` is run by the bridge after every builder turn, and its exit code and last lines go
    to the supervisor. It is owner-only.
  - The supervisor's message also lists which files the new commits touched.
- **Unattended runs.**
  - Desktop notifications on macOS and/or `[notify] command` when a run pauses, completes, or needs the
    owner.
  - Per-turn cost and token usage go into the events, `status` and the report. They are API prices,
    which a subscription does not bill.
  - `budget.max_cost_usd` caps a run.
  - Logs over 64 MB rotate at a run's start, and `status` and `logs` read only the tail.
- **Designed earlier, now built.**
  - The planner rotates at `planner_max_context_tokens`.
  - Stored sessions are checked against the repo at every start.
  - `check` warns when the opencode server was started with a different `env_file`.
  - `check` says how `git push` is blocked.
  - STOP is checked every 5 s during waits.

## 9. Configuration: `bridge.toml`

- Read with `tomllib`.
- Looked up in the repo root, or passed with `--config PATH`.
- Unknown keys are errors, so a typo cannot silently fall back to a default.
- TOML has no null: optional keys are omitted, and the template shows them commented
  out.
- Durations are strings (`"30m"`, `"3h"`, `"1d"`). Paths are relative to the repo and
  stored absolute.
- `init` writes this file from a text template; in `new`, the planner writes it. The
  values below are the defaults.

```toml
version = 1

[project]
name = "FillingQA"
repo = "."                               # relative to this file
prd = "docs/PRD.md"
rules = ["CLAUDE.md"]                    # AGENTS.md is a symlink to CLAUDE.md
decisions = "docs/DECISIONS.md"          # the ledger
open_items = "docs/OPEN.md"
results = "docs/RESULTS.md"              # the builder's results register (6.2)
mode = "autonomous"                      # autonomous | escalate
# worklog = "docs/WORKLOG.md"            # used in builder handoffs when present
# phases = "PRD section 14"              # where the phase plan is, if not under "Phase" headings
# supervisor_rules = "docs/SUPERVISOR.md"   # extra supervisor principles (migrated projects)
# env_file = ".env"                      # opt-in; builder environment only
# verify = "make test"                   # run by the bridge after each builder turn (8.4); owner-only
# verify_timeout = "15m"

[planner]
engine = "claude-code"                   # claude-code | opencode
model = "claude-fable-5-1"
variant = "max"                          # claude --effort; opencode --variant
timeout = "1h"

[supervisor]
engine = "claude-code"
model = "claude-fable-5-1"
variant = "xhigh"
timeout = "30m"

[builder]
engine = "claude-code"                   # opencode stays selectable
model = "claude-opus-5-5"                # on opencode: "anthropic/claude-opus-5-5"
timeout = "3h"

# [opencode]                             # only when a role uses opencode
# port = 4099                            # required then (no default; DEC-001); one per project; init picks a free one
# accept = "1.18"

[billing]
mode = "subscription"                    # subscription | api-key   (owner-only)

[git]
push = "never"                           # never | allowed          (owner-only)

[waits]
max = "24h"

[budget]                                 # reaching a cap pauses with a summary
# max_exchanges = 300                    # per run invocation
# max_wall_time = "72h"                  # per run invocation, waits included
max_unchanged_exchanges = 12             # consecutive, outside waits
max_replans_per_phase = 3
# max_cost_usd = 50                      # per run invocation, at the engines' API prices (8.4)

[rotation]
builder_max_context_tokens = 600_000
builder_on_phase_complete = true         # at the supervisor's PHASE COMPLETE
supervisor_max_context_tokens = 400_000
supervisor_on_phase_complete = true
planner_max_context_tokens = 400_000
phase_complete_pattern = '(?i)\bphase\s+[\w.-]+\s+(?:is\s+)?complete\b'   # builder claims: a hint only

[safety]
danger_commands = []                     # extra regexes over builder shell commands
caffeinate = true                        # macOS
sandbox = "auto"                         # auto | on | off; owner-only (8.4)
# sandbox_writable = []                  # extra writable folders for the builder

[notify]                                 # 8.4
desktop = true
# command = "..."                        # gets AGENT_BRIDGE_EVENT, _TITLE, _MESSAGE, _PROJECT, _REPO
```

A role's `variant` default (`max`, `xhigh`) applies only when the role keeps its default
engine and model; effort levels are model-specific, so any other model runs at the
engine's default unless `variant` is set (DEC-015).

Fixed internal constants (documented, not configurable):
- reply cap 24,000 chars;
- backoff 60 s to 30 min;
- a pause after 3 timeouts, 3 questions or 10 errors in a row;
- a 15 s poll interval;
- a 2-minute reset margin.

**The builder's default model is the Opus id Claude Code actually serves.** In local Claude
Code sessions, `claude-opus-5-5` appears as the served `message.model` from 2026-09-24 to
2026-10-01; `claude-opus-5` was last served on 2026-09-18. The served-model check (4.3)
flags any turn served by a different id, and the smoke tests confirm the id live.

## 10. State directory: `.bridge/`

Compatible with the old layout, so migration keeps the logs.

| File | Content |
|---|---|
| `state.json` | the machine state (section 7), the approved contract hashes; `schema: 1` |
| `lock` | flock plus pid, start time, argv |
| `events.jsonl` | structured events with origin and recipient; the source for `status` and `logs -f` |
| `loop.log` | human transcript: every message as delivered, labels included; appended |
| `review.log` | owner-facing warnings, in the old `=== … ===` format, appended |
| `console.log` | everything printed, appended, with a launch marker per run (L16) |
| `serve.log` | the opencode server's output, appended |
| `sessions.json` | every session the bridge created or adopted: role, engine, id, title, directory, created, retired |
| `plan/` | interview questions and answers, the drafts as returned, `changes/PC-NNN.json` and `.diff` |
| `turns/NNNN-<role>.jsonl` | raw event streams; the 200 most recent kept |
| `builder_last.md` | the last builder report (kept for compatibility) |
| `inbox/` | owner messages, approvals and decisions docs not yet delivered |
| `STOP`, `STOP_NOW` | stop requests |
| `unsent_reply.md` | the pending message saved on stop |
| `PAUSED.md` | the pause summary |
| `owner_todo.md` | output of `review` |
| `reports/`, `report.md` | owner-review reports (6.10); `report.md` is the latest |

`init` and `new` add `.bridge/` to `.gitignore`, and `.omo/` when a role uses opencode. Both
are excluded from the repo snapshots behind the tripwire and the change counter (DEC-014).

## 11. CLI

Every command takes `--repo PATH` (default: the cwd's git root) and `--config PATH`.

| Command | What it does |
|---|---|
| `new "<idea>" [--repo PATH] [--auto-approve] [--planner E:M] [--supervisor E:M] [--builder E:M]` | The planner flow (6.1). |
| `approve [PC-n …] [--reject PC-n --reason TEXT] [--done PHASE … [--reason TEXT]] [--forever \| --loop N \| --background \| --no-run]` | Approve the plan in `PLAN_REVIEW`, adopt an existing contract, or approve or reject waiting plan changes (6.4, 6.6). `--done` records phases finished before agent-bridge as complete: an OWNER DECISION ledger entry each, never claimed as verified (DEC-019). |
| `decide DOC [--auto-approve] [--forever \| --loop N \| --background \| --no-run]` | Owner decisions through the planner (6.7). |
| `init [--planner E:M] [--supervisor E:M] [--builder E:M] [--port N] [--name NAME] [--write-rules] [--adopt-legacy]` | For an existing repo: write `bridge.toml` from the text template (detecting existing docs, and picking a free port if a role uses opencode), add `.bridge/` (and `.omo/` when a role uses opencode) to `.gitignore`, and suggest the `AGENTS.md` symlink. It warns if the installed opencode is not 1.18.x (4.2). `--write-rules` adds the generated block to `CLAUDE.md` and prints the diff. `--adopt-legacy` imports an old `tools/bridge.py` `.bridge/` folder (section 16). Never overwrites `bridge.toml`. |
| `run [--forever \| --loop N] [--kickoff MSG\|@file] [--new-supervisor] [--new-builder] [--confirm-each] [--background]` | Run the loop on the approved contract. With neither `--forever` nor `--loop`, one exchange. At start it warns if opencode is not 1.18.x, and refuses if a role uses it (4.2). `--kickoff` is an owner message. `--confirm-each` shows each supervisor reply for send, edit or discard (the old manual mode). `--background` detaches, writes to `console.log`, and prints the pid. |
| `status [--json]` | See below. |
| `stop [--now]` | 8.2. |
| `pin --planner ID \| --supervisor ID \| --builder ID \| --new-planner \| --new-supervisor \| --new-builder` | Adopt or reset a session. It checks that the session belongs to this repo, and refuses while a bridge is running. |
| `say "message" \| @file [--to both\|builder\|supervisor]` | In `INTERVIEW`: the answers to the planner, which runs the drafting turn. Otherwise: an owner message for the next exchange, which interrupts a wait; by default both agents get it verbatim. |
| `review [--template PATH]` | The owner to-do: `AUTONOMOUS DECISION` and `AWAITING OWNER` ledger entries, OWNER-BLOCKED items (headings, table rows and status lines; RESOLVED skipped; all three projects' formats), and waiting plan changes. Writes `.bridge/owner_todo.md` and prints it. `--template` writes an `OWNER_REVIEW.md` skeleton (D1..Dn and "Done when"). |
| `report [--out PATH]` | Write the owner-review report now (6.10). It is also written automatically at PROJECT COMPLETE. |
| `logs [-f] [--transcript \| --review \| --console \| --serve \| --events] [-n N]` | Default: rendered events. `-f` follows. |
| `engines [--planner E:M] [--supervisor E:M] [--builder E:M] [--port N]` | With no flags, shows each role's engine and model. With flags, rewrites only those roles' lines in `bridge.toml` (comments kept), turns on `[opencode] port` when a role needs it, validates the result, closes sessions on the old engine, and re-approves the contract with an "Engines changed by the owner" ledger entry. When other contract files also changed since approval it re-approves nothing and says so. Refused while a bridge runs. The UI's `e` uses it. |
| `ui [--all]` | The terminal UI (section 18). `agent-bridge` with no arguments opens it when stdin and stdout are terminals, and prints the help otherwise. |
| `check` | Config; binaries and versions; `autoupdate`; auth (`claude auth status`); the server port; read-only per role; git identity; `.gitignore`; `AGENTS.md`; contract hashes and drift; the configured docs. No model calls. |

`status [--json]` shows:
- the state: planning, running, sleeping, waiting or paused, with the reason;
- the current exchange and the turn's age;
- the last verdict;
- an active wait or sleep, and when it ends;
- the sessions, with context size;
- read-only enforcement per role;
- the contract (approved at, drift);
- waiting plan changes and pending owner messages;
- warnings since launch, and budget use;
- open DECISIONS NEEDED.

Exit codes: 0 complete or stopped; 2 configuration or unsupported; 3 paused or waiting
for the owner; 130 interrupted.

## 12. Live view

The foreground `run` and `logs -f` share one renderer over `events.jsonl`, in plain ASCII.
Every line names the role it came from:

```
[22:20:29] == exchange 3 == builder (opencode anthropic/claude-opus-5-5, ses_f08e8379)
[22:20:31]   builder > bash: git status --porcelain
[22:20:40]   builder > edit: scripts/eval_run.py
[22:24:10]   builder | I'm making `--report` stdout-only by extracting a function ...
[22:31:02]   builder + done in 10m33s, ctx 120,541 tokens, 2 new commits
[22:31:03]   supervisor (claude-code claude-fable-5-1, read-only enforced)
[22:31:20]   supervisor > Read ~/FillingQA/docs/WORKLOG.md
[22:32:40]   supervisor VERDICT: Builder committed F-144 (65c442f) ... Sound.
[22:32:40]   bridge   WAIT FOR PID 64410 (max 24h): sleeping, no model calls
```

Text lines are clipped in the live view; the full text is in `loop.log` and the turn
files. For an opencode role, the start banner also prints the `opencode attach` command,
for watching inside opencode itself.

## 13. Budget

Checked at every step boundary. The caps:
- exchanges, or wall time, for this invocation;
- the unchanged streak, outside waits;
- re-plans per phase.

When one is reached, the run pauses. `PAUSED.md` records which cap, the counters, the
last verdict, the open decisions, and the command to continue. It never continues
silently.

## 14. Testing

- pytest. Every test gets a temporary repo (`git init` in `tmp_path`) and a temporary
  `.bridge/` (L8). No test makes a model call or reads the real
  `~/.local/share/opencode`.
- A fake clock replaces sleeping, so waits, limits and backoff run instantly.
- **Unit tests:**
  - all three output grammars: every directive and form, sentinel placements, the
    no-REPLY fallback;
  - reset parsing, for every format quoted in INVENTORY L2;
  - wait conditions, including pid reuse;
  - config validation, including the owner-only settings;
  - message composition and labels;
  - find/replace edits and diff rendering;
  - materiality rules;
  - `review` extraction over fixtures in all three projects' formats.
- **Planner with FakeBackend:**
  - interview answered inline, with `say`, and under `--auto-approve`;
  - drafting validation with one retry, then a pause;
  - `approve` hashing and status changes;
  - adopting an existing contract;
  - every cell of the re-planning outcome table;
  - a threshold edit never applied automatically;
  - `max_replans_per_phase`;
  - `decide`, with a fresh supervisor;
  - contract drift flagged and not reverted;
  - the blocked set built from `AFFECTS` and from the edit locations, and cleared on
    approve or reject;
  - SCOPE: the nudge, then the held reply and the pause;
  - the re-plan cap: the OWNER-BLOCKED item, the paused phase, the refusal, and the
    reset by `decide`.
- **Engine with FakeBackend:**
  - kickoff to completion;
  - STOP saving the pending message on every path;
  - WAIT for each form, with its wake notes and interruptions;
  - limit sleep and resend;
  - the auth pause and its exit code;
  - `Unsupported` exiting without retrying;
  - the timeout and question bounds;
  - the budget pause and summary;
  - rotation decided once (C1);
  - a fresh supervisor after COMPLETE;
  - an owner `say` reaching each agent once;
  - crash recovery from `in_flight`;
  - a second instance refused by the lock;
  - origin labels in `loop.log` and `events.jsonl`;
  - `PHASE COMPLETE` rotating the builder (with its handoff) and the supervisor, once;
  - the report written at PROJECT COMPLETE, with every link resolving to a real commit
    or ledger line in the temporary repo.
- **Backend plumbing:** command lines for each role and engine; event parsing from
  recorded fixtures; timeout leading to abort; question polling, reject and abort; the
  retry status leading to `SessionLimit`. These use fake `opencode` and `claude`
  executables and a fake HTTP server. The fixtures are recorded during the live smoke
  tests and scrubbed of personal data.

## 15. Dependencies and packaging

- **Runtime: none.** `bridge.toml` is read with `tomllib` (Python 3.12). Everything else
  is stdlib: `subprocess`, `threading`, `urllib`, `sqlite3`, `zoneinfo`, `fcntl`,
  `argparse`, `json`, `difflib`, `hashlib`.
- **Development:** pytest.
- **Packaging:** `pyproject.toml` with the setuptools build backend (a build-time tool,
  not a runtime dependency), `requires-python >= 3.12`, and the console script
  `agent-bridge`.
- **Install:** `uv tool install --editable ~/agent-bridge`, or `pip` under
  `/opt/homebrew/bin/python3.12`. The `python3` on PATH is 3.9.

## 16. Migration plan (detail in `docs/MIGRATION.md`; FillingQA moved on 2026-10-06, the other two have not)

Per project, while its bridge is stopped:

1. **Config.** Write `bridge.toml`:
   - the same port (4096, 4097, 4098) and models, with the builder on opencode;
   - `decisions = "docs/TRADEOFFS.md"`;
   - the supervisor either stays on opencode at first (current behaviour, keeping its
     session) or moves to Claude Code for enforced read-only (a fresh supervisor);
   - a planner configured for later re-plans and `decide`.
2. **Contract.** `agent-bridge approve` adopts the existing `docs/PRD.md` and `CLAUDE.md`
   as the approved contract. Move the project parts of `SYSTEM` and `AUTONOMOUS`
   verbatim into `docs/SUPERVISOR.md`. `init --write-rules` adds the generated block to
   `CLAUDE.md` after showing the diff.
3. **State.** `agent-bridge init --adopt-legacy` (or `pin --builder $(cat .bridge/session)`,
   and the same for `--supervisor`). An adopted supervisor session gets the new role text
   once, in its first turn under agent-bridge (DEC-016). A role whose engine is later
   changed in `bridge.toml` starts a fresh session instead of resuming the other engine's. Keep `loop.log`, `review.log`, `console.log` and `serve.log`; the
   bridge appends to them. `unsent_reply.md` and `builder_last.md` become the pending
   state. The kickoff files stay where they are.
4. **Environment.** X's `.env` becomes `project.env_file = ".env"`. N's question polling
   and abort are built in. X's `opencode.json` question deny can stay.
5. **Retire.** `tools/bridge.py` stays in each repo until the owner removes it. Nothing is
   deleted by the migration.

The pinned TUI-created builder sessions lack opencode's question deny (L5). The question
polling covers them, and `--new-builder` replaces them with a session that has the deny.

## 17. Build order

Small commits, each with its tests:

1. Package skeleton, TOML config, statedir and journal.
2. Protocol (all three grammars), limits and waits.
3. Backend base, FakeBackend and the engine core (turns, labels, STOP, completion).
4. Engine: waits, limits, pauses, budget, rotation, `say`, crash recovery.
5. Planner and contract: `new`, interview, drafting, `approve`, ledger, re-planning,
   `decide`, drift.
6. ClaudeCodeBackend.
7. OpencodeBackend and server management.
8. CLI commands, the live view, `review` and `report`.
9. **Live smoke tests**, with Haiku (`claude-haiku-4-5-20251001`) in temporary repos
   under `/tmp`:
   - before each one, `pgrep -fl tools/bridge.py`; if anything matches, that test is
     skipped and the skip noted;
   - test A: all three roles on Claude Code;
   - test B: all three roles on opencode, on a free port outside 4096–4098;
   - each runs `new --auto-approve` and then two exchanges;
   - afterwards, only the sessions they created are deleted, by exact id, after checking
     title and directory read-only;
   - the served-model check confirms the model ids live, including Opus for the
     builder default;
   - the Claude Code tests need a working `claude login` (it had expired on
     2026-10-06, see 4.3); without one they are skipped and noted;
   - the report says whether opencode lost its login.
10. `docs/MIGRATION.md` and the README.

## 18. Terminal UI

Added 2026-10-06 at the owner's request (DEC-017): one command, `agent-bridge`, opens a
full-screen terminal UI. It uses only `curses` from the standard library.

- **Scope.** It opens on the current folder's project. ctrl-a lists every project in the
  registry, `$XDG_STATE_HOME/agent-bridge/projects.json`; any command that loads a project
  records it there. Everything the CLI does is reachable:
  - new project and the planner's interview;
  - init and adopt;
  - run, stop, message, approve (plan, plan changes, re-approval), decide;
  - fresh sessions, check, report, logs.
- **It reads and launches; it never drives the engine.** The view reads `.bridge/`:
  - `state.json` and `review.log` are re-read when their mtime changes;
  - `events.jsonl` is tailed from a byte offset;
  - the lock tells it whether a bridge is running.

  Every action runs an `agent-bridge` subcommand as a detached process (new session,
  output under `$XDG_STATE_HOME/agent-bridge/ui/`), and its result becomes a toast.
  Closing the UI never stops a run, and the CLI stays the single implementation of every
  action (DEC-018).
- **Layout.**
  - A header with the state chip.
  - Three role pods: engine and model, read-only enforcement, activity, context gauge.
  - A signal line that shows which way the current message travels.
  - The next-step line.
  - The event stream.
  - Owner, phases and alerts cards.
  - Contextual keycaps.

  Forms, confirmations, a tabbed document viewer (search, `$EDITOR`), a command palette
  and pick lists are overlays.
- **Drawing.** Screens draw into an off-screen canvas that understands wide characters.
  Only changed rows are copied to curses. The canvas also renders to HTML, which
  `scripts/tui_preview.py` uses for previews without a terminal.
- **Tests.**
  - Units for text width, the canvas, editing and the watcher.
  - Every action against the exact CLI argv it launches.
  - Rendering at sizes from 150×42 down to the 60×16 minimum.
  - A run of the real curses app in a pseudo-terminal.

