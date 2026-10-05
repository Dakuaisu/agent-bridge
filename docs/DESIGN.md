# agent-bridge design

Status: proposal, for review before any code is written. The failure IDs (L1–L21,
C1–C6) refer to `docs/INVENTORY.md`.

## 1. Goals

- One tool, installed once, that runs a builder and a supervisor against any repo:
  a `bridge.yaml` per project, no per-project Python.
- Either role on either backend (opencode 1.18.x or Claude Code), in any combination.
- Safe to leave alone for days. Every stop, wait, pause and warning is visible in
  `status` and in the logs. It never loops on a hard error, and it never makes model
  calls just to pass time (L1).
- Everything that worked in the three copies is kept (INVENTORY §5). Every failure
  in INVENTORY §3 has a mechanism and a test.

Non-goals: a TUI, multiple builders, remote machines, managing opencode installs, and
deleting sessions (the tool records the sessions it creates and never deletes any).

## 2. Architecture

```
agent_bridge/
  cli.py          argparse entry point; one function per command
  config.py       load and validate bridge.yaml into frozen dataclasses
  statedir.py     .bridge/ layout, atomic JSON writes, lock, inbox, STOP
  journal.py      events.jsonl (structured), loop.log, review.log, console.log
  engine.py       the loop: a persisted state machine (section 5)
  protocol.py     parse supervisor output and builder reports; compose messages
  prompts.py      role text, notes, handoffs: generic text plus config and repo docs
  waits.py        WaitSpec and its conditions (UNTIL, FOR PID, FOR FILE)
  limits.py       reset-time parsing for every observed limit message
  repo.py         git: fingerprint, delta, new commits, trailer and push checks
  budget.py       caps and the pause summary
  owner.py        review (owner to-do), the decisions template, apply-decisions, new
  live.py         renderers shared by foreground run, `logs -f` and `status`
  clock.py        Clock protocol (real, and fake for tests)
  backends/
    base.py       Backend, Reply, Capabilities, errors
    opencode.py   OpencodeBackend and OpencodeServer
    claude_code.py
    fake.py
```

Single-threaded engine. Each backend turn runs a subprocess whose stdout is read by a
reader thread (streaming events to the journal and the live view), plus, for opencode,
a monitor thread that polls the server (section 3.2). The engine thread waits on the
turn with a deadline and a cancel flag (set by `stop --now` or a detected question or
limit).

## 3. Backends

### 3.1 Interface

```python
@dataclass(frozen=True)
class Capabilities:
    read_only: str          # "enforced: --tools Read,Grep,Glob" or
                            # "instruction-only, with repo tripwire and tool-call audit"
    question_guard: str     # how an interactive question is prevented or caught
    live_attach: str | None # e.g. "opencode attach http://127.0.0.1:4096 --dir … --session …"

@dataclass
class Reply:
    text: str               # builder: all text parts in order; supervisor: see 4.3
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

### 3.2 OpencodeBackend (1.18.x)

- **Version gate.** `opencode --version` must match `opencode.accept` (default `1.18`).
  Anything else raises `Unsupported` before any model call, explaining why 2.x is
  refused (no `--attach`/`--dir`, the omo plugin cannot load, Anthropic bills
  third-party apps from extra usage) and how to pin 1.18.30. It warns if
  `~/.config/opencode/opencode.jsonc` lacks `"autoupdate": false`. Any "Unrecognized
  flag" in stderr is `Unsupported`, never transient (L6).
- **Server.** One `opencode serve --hostname 127.0.0.1 --port <port>` per project,
  cwd = repo, started detached (`start_new_session=True`) so it outlives the bridge.
  Output is appended to `serve.log` with a timestamped start marker. If the port is
  already open, `GET /global/health` must answer as opencode with an accepted version;
  otherwise the bridge stops with a message and touches nothing. The bridge never
  stops a server.
- **Turn.** `opencode run --attach URL --dir REPO --format json --auto -m PROVIDER/MODEL
  [--variant V] (--session ID | --title "<project> <role> <date>") -- MESSAGE`. Every
  message starts with a `[bridge]` line, so it can never begin with `-`. The `--`
  separator is used if the smoke test confirms 1.18.30 accepts it. JSON events
  (`step_start`, `text`, `tool_use`, `step_finish`, `error`) are streamed. The session
  id is persisted from the first event, not at the end.
- **New sessions are created by `opencode run`**, which gives them the `question`,
  `plan_enter` and `plan_exit` deny rules (L5). A session adopted with `pin` lacks
  those rules; `pin` and `status` say so.
- **Monitor thread** (every 15 s while a turn runs):
  - `GET /question?directory=REPO`: a question for this session or its children is
    rejected (`POST /question/{id}/reject`), the session aborted, and `QuestionAsked`
    raised.
  - `GET /session/status?directory=REPO`: status `retry` whose `next` is more than 5
    minutes away, or whose message matches a limit pattern ("pool exhausted", "usage
    limit", "rate limit"), aborts the turn and raises `SessionLimit(reset_at=next)` or
    `RateLimited` (L2). Shorter retries are left to opencode.
  - New `serve.log` lines since the turn began are matched for limit and 401 messages
    ("next account free in 1h 45m", "Account usage window rejected … resets in N
    seconds"), as a second source for the same classification.
- **Abort for real.** Timeout, cancel, question or limit: `POST
  /session/{id}/abort?directory=REPO`, then end the client (SIGTERM, then SIGKILL
  after 30 s). Before every send the session's status is read; a busy session is
  aborted first, so retries never stack as queued messages (L4).
- **Reply text.** Builder: every `text` part in order, which matches the old default
  output. Supervisor: section 4.3.
- **Served model.** Assistant messages created during the turn are read from
  `opencode.db` with `sqlite3` in `mode=ro` (URI) plus `PRAGMA query_only`. The session
  id is validated against `^ses_[A-Za-z0-9]+$` and passed as a bound parameter.
- **Context size.** From the `tokens` on the last `step_finish` event, falling back to
  the DB.
- **Read-only.** Instruction-only: the claude-bridge plugin overrides permission
  denies, so read-only cannot be enforced. It is backed by the repo delta and the
  supervisor's own `tool_use` events (any `write`, `edit`, `patch` or `bash` call by a
  read-only role goes to `review.log`).
- **Agents.** Never passes `--agent` (omo drops custom agents and falls back to
  Sisyphus). The role travels in the message; the model is set with `-m`. Never
  `--pure` (401).
- **Unverified so far.** The routes (`/global/health`, `/session/status`, `/question`,
  `/question/{id}/reject`, `/session/{id}/abort`) and the `retry` status shape
  (`attempt`, `message`, `next`) come from the 1.18.30 binary. The smoke test checks
  their response shapes against a live server before the backend relies on them.

### 3.3 ClaudeCodeBackend

- **Turn.** The prompt is passed on stdin.

  ```
  claude -p --output-format stream-json --verbose --model MODEL [--effort V]
    (--session-id UUID | --resume UUID) --append-system-prompt ROLE
    --permission-prompts none -n "<project> <role>"
  ```

  - Builder: cwd = repo, `--permission-mode bypassPermissions` (the counterpart of
    opencode's `--auto`), `--disallowedTools AskUserQuestion`. With `git.push: never`,
    `--settings` adds deny rules for `git push`. The smoke test checks whether deny
    rules hold under `bypassPermissions`; if they do not, no-push stays an instruction
    backed by detection, and `check` says so.
  - Read-only role: `--tools Read,Grep,Glob --allowedTools Read,Grep,Glob`. This is
    enforced.
  - Supervisor cwd: a neutral per-project directory outside the repo
    (`~/.local/state/agent-bridge/<project-id>/supervisor/`) with `--add-dir REPO`.
    That keeps the builder's `CLAUDE.md` out of the supervisor's context (the same
    lesson as FillingQA's `claude_cli` generator). The smoke test confirms reads
    through `--add-dir` work; if they do not, the fallback is cwd = repo, as in the old
    claude path.
- **Sessions.** The bridge generates the UUID and persists it before the first call,
  then uses `--resume UUID`. Session files live under `~/.claude/projects/`, keyed by
  cwd, so each role always runs from the same cwd.
- **Environment.** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed unless
  `billing: api-key`. `CLAUDECODE` and related variables are removed so a bridge
  launched from a Claude Code terminal is not treated as nested. The `system/init`
  event's `apiKeySource` is checked against the billing mode; a mismatch is
  `Unsupported` (it would bill the wrong account).
- **Errors.** A `result` with `is_error` is classified:
  - limit text ("You've hit your session limit · resets 9:40pm (Asia/Calcutta)",
    "usage limit reached", weekly variants): `SessionLimit`, with the reset time parsed
    by `limits.py`;
  - 401, "invalid api key", "please run /login", "OAuth token expired": `AuthFailed`;
  - 429 or overloaded: `RateLimited`;
  - anything else: `TransientError`.

  A timeout kills the process group. There is no server-side turn.
- **Health.** `claude --version`; `claude auth status` (JSON: `loggedIn`, `authMethod`,
  `subscriptionType`), which makes no model call.
- **Served model.** `message.model` on each assistant event. `modelUsage` is not used
  for this, because Claude Code also calls small models for housekeeping.
- **No SDK.** The Claude Agent SDK drives this same CLI. Calling the CLI directly keeps
  the tool stdlib-only.

### 3.4 FakeBackend

Scripted from a list of steps or a callable: a reply (text, served model, tool calls,
context tokens, optional file changes applied to the temp repo), or an error to raise,
optionally after advancing the fake clock. It records every message it was sent, so
tests can assert on labels, notes and ordering. It is the only backend the test suite
calls. Subprocess plumbing (timeouts, aborts, streaming) is tested with fake
`opencode` and `claude` executables and a stdlib `http.server` standing in for the
opencode server.

## 4. Message protocol

### 4.1 Sources are labelled (L11, L12)

Every message carries labelled blocks, so neither agent mistakes bridge text for an
owner instruction.

Builder message, in this order (empty blocks omitted):

```
[bridge] Headless run: never use a question or ask-user tool; put questions under
DECISIONS NEEDED. To pause for a job or a time, end your report with one line:
WAIT FOR PID <n> | WAIT FOR FILE <path> | WAIT UNTIL <ISO-8601 time>.
[owner] (verbatim; binding; the supervisor has it too)
<owner text>
[bridge] <one note: resume after timeout | question rejected | wait over | cut off by a
usage limit | fresh session handoff>
[supervisor]
<the supervisor's REPLY>
```

Supervisor message per turn:

```
[bridge] You are the read-only SUPERVISOR of <project> (<how read-only is enforced>).
Absolute paths under <repo> only. Output VERDICT, optional directive lines, REPLY.
[bridge] Re-verify the builder's claims against the repo this turn. Your memory of
earlier turns is context, not evidence.
[bridge] Since your last turn: HEAD a1b2c3d..e4f5a6b (3 commits: <subjects>); working
tree: 2 modified, 1 untracked. Builder turn: 41m, ended normally.
[bridge] The builder asked for: WAIT FOR PID 64410. It is honoured after your reply
unless you write NO WAIT or a different WAIT line.
[bridge] The builder listed 2 DECISIONS NEEDED; answer each in your REPLY.
[owner] (verbatim; binding; already delivered to the builder) <owner text>
===== BUILDER =====
<builder report, last 24,000 chars; the full text is in the turn file>
===== END =====
```

The first supervisor message of a session also carries the role (section 6.3).

### 4.2 Supervisor output grammar

```
[optional preamble]
PROJECT COMPLETE                      optional (see below)
VERDICT: <one line>
WAIT UNTIL <ISO-8601>        [MAX <duration>]       optional directives, one per line
WAIT FOR PID <n>             [MAX <duration>]
WAIT FOR FILE <path>         [MAX <duration>]
NO WAIT
ROTATE BUILDER
ESCALATE: <question>                  honoured only in mode: escalate
REPLY:
<message for the builder>
```

- The output is split at the first `REPLY:` line. Directives are read only from the
  part above it, so a WAIT quoted in the reply body does nothing.
- Lines are normalized before matching (markdown `*_#>\`` and a leading `- ` stripped).
- **Sentinel.** `PROJECT COMPLETE` counts as a standalone line above `REPLY:` (a
  preamble is tolerated), or as the first non-empty line of the body. Deeper in the
  body it is ignored, with a warning (C4).
- **No `REPLY:`.** One nudge in the same session. If the output is still malformed,
  the whole text is sent as the reply and a warning is logged (C5).
- **Empty output.** One nudge in the same session, then `TransientError`.
- Durations: `90s`, `30m`, `2h`, `1d`, `1h30m`; a bare number means seconds.

### 4.3 Supervisor reply text

The text of the last step that contains `REPLY:`. If no step does, all text parts are
joined. That keeps the old "last step" behaviour and survives a VERDICT written in an
earlier step.

### 4.4 Builder report signals

- `WAIT …` lines anywhere in the report are a wait request (the last one wins). The
  supervisor sees it; it is honoured unless the supervisor writes `NO WAIT` or its own
  WAIT. The builder knows the pid or reset time; the supervisor keeps the veto.
- The `DECISIONS NEEDED:` section is extracted, highlighted to the supervisor, and shown
  in `status`.
- A phase claim matching `rotation.phase_complete_pattern` (default
  `(?i)\bphase\s+[\w.-]+\s+(?:is\s+)?complete\b`) marks a phase boundary. The decision
  is made once per builder report and stored, so retries do not repeat it (C1).

## 5. Loop state machine

The state lives in `.bridge/state.json`, written atomically (temp file plus rename)
before each action, so a crash or restart resumes exactly where it stopped.

```
 IDLE ──(kickoff / say / apply-decisions)──► BUILDER_TURN ◄───────────────────────┐
                                                  │ builder reply                  │
                                                  ▼                                │
                                           SUPERVISOR_TURN                         │
                                                  │ parsed output                  │
                 ┌───────────────┬────────────────┼─────────────────┐              │
                 ▼               ▼                ▼                 ▼              │
          REPLY, no WAIT        WAIT       PROJECT COMPLETE     ESCALATE           │
                 │               │                │          (escalate mode)       │
                 │               ▼                ▼                 ▼              │
                 │            WAITING          COMPLETE     PAUSED(escalation)     │
                 │               │ condition met, MAX reached or owner message     │
                 └───────────────┴─────────────────────────────────────────────────┘

 Any turn ── SessionLimit / RateLimited ──► SLEEPING(until reset + 2 min) ──► same turn
 Any turn ── transient error ─────────────► SLEEPING(backoff) ──────────────► same turn
 Any turn ── AuthFailed ──────────────────► PAUSED(auth)
 Any turn ── Unsupported ─────────────────► exit 2 (never loops)
 Any step boundary ── STOP ───────────────► PAUSED(stop), pending message saved
 Any step boundary ── budget cap ─────────► PAUSED(budget), summary written
```

| State | Persisted fields | Leaves when |
|---|---|---|
| `IDLE` | — | a kickoff, `say` or `apply-decisions` arrives |
| `BUILDER_TURN` | pending message parts, attempt, `in_flight` | reply (to `SUPERVISOR_TURN`) or error (table 6.1) |
| `SUPERVISOR_TURN` | path of the builder report under review, attempt | parsed output |
| `WAITING` | WaitSpec, start, deadline, pending reply, who asked | condition met, MAX reached, owner message, STOP |
| `SLEEPING` | until, reason, resume state | time reached, STOP |
| `PAUSED` | reason, detail, resume state | `agent-bridge run` again (the process has exited) |
| `COMPLETE` | time, summary file | new owner work, which always gets a fresh supervisor (L15) |

**PAUSED means the process exits** (code 3) after writing `.bridge/PAUSED.md`: the
reason, counters, last verdict, open DECISIONS NEEDED, where the pending message is,
and the exact command to continue. Nothing runs while paused. `agent-bridge run`
resumes from the saved state.

**Crash recovery.** A state saved with `in_flight` set means the bridge died mid-turn.
On restart the session is checked; if it is busy, it is aborted, and the message is
resent with "the bridge restarted while you were working; check git status before
continuing".

### 5.1 WAIT

- **Who.** The supervisor, as a directive. The builder, as a request in its report,
  honoured unless the supervisor writes `NO WAIT` or its own WAIT.
- **Conditions, checked every 15 s with no model calls:**

  | Form | Satisfied when | Notes |
  |---|---|---|
  | `WAIT UNTIL <time>` | the time is reached | a time with no offset is local; a time in the past is satisfied at once |
  | `WAIT FOR PID <n>` | the process is gone | the start time (`ps -o lstart=`) is recorded, so a reused pid counts as gone; a pid that is not running at the start is satisfied at once |
  | `WAIT FOR FILE <path>` | the path exists | a relative path is resolved against the repo |

- **Bounds.** `MAX` defaults to `waits.max` (24 h). Longer requests are clamped and the
  clamp is logged. A multi-day wait (for example a weekly quota) therefore costs at most
  one exchange per day.
- **Wake-up.** The next builder message gets `[bridge] The wait (WAIT FOR PID 64410) is
  over: the process exited at 03:31. Check the result before anything else.` (or "MAX
  reached without the condition"), followed by the supervisor's REPLY from the turn that
  set the wait. There is no extra supervisor call.
- **Interruptions.** STOP ends a wait at once and saves the pending reply. An owner
  `say` ends it and is delivered with a note that the condition was not met.
- **Backstop for supervisors that do not use WAIT (L1).** After 3 consecutive
  exchanges with no repo change and no active wait, the supervisor gets a `[bridge]`
  note: "if the builder is waiting, use WAIT instead of acknowledging". After 5, the
  bridge itself sleeps (30 min doubling to 2 h, X's backstop) and logs it. After
  `budget.max_unchanged_exchanges` (default 12), the run pauses.

### 5.2 Usage limits

- A `SessionLimit` with a known reset sleeps until the reset plus 2 minutes, with no
  model calls, and then retries the same turn. If a builder turn was cut off, the
  resend says so: "you were cut off by a usage limit at 05:01; check git status and the
  log, then continue".
- An unknown reset backs off 15 min, doubling to 1 h, one request per attempt.
- 48 hours of consecutive limit sleeping pauses the run.
- Sleeps are visible in `status` ("sleeping until 17:30, session limit,
  claude-code/builder").

### 5.3 Rotation (L19, L15)

- **Builder.** When the last request's context reaches
  `rotation.builder_max_context_tokens` (default 600k), when the supervisor writes
  `ROTATE BUILDER`, or at a phase boundary if `builder_on_phase_complete` is set, the
  next builder message opens a new session. The supervisor is told one turn ahead, so
  it can make its REPLY self-contained. The new session gets a `[bridge]` handoff that
  lists, from config, the rules files, the spec and the phases section, the worklog
  (from its end back to the current phase), the open-items doc and the owner decisions
  doc, plus `git log --oneline -20`, `git status` and any background jobs the worklog
  records. The old session is retired in `sessions.json`, never deleted.
- **Supervisor.** Rotated at a phase boundary (on by default, as in all three copies),
  at `supervisor_max_context_tokens`, on `--new-supervisor`, and always before new work
  after `COMPLETE`. A fresh supervisor's first message includes the last verdict and
  "a previous supervisor session reviewed exchanges 1–N; re-verify from the repo".
- A timeout never rotates a session (L3).

## 6. Errors, safety and prompts

### 6.1 Error handling

| Error | Builder turn | Supervisor turn |
|---|---|---|
| `Timeout` | aborted server-side; resent with a resume note; 3 in a row pauses (L14) | aborted; retried in the same session after backoff; 3 in a row pauses |
| `QuestionAsked` | rejected and aborted; resent with the questions and "answer them under DECISIONS NEEDED"; 3 in a row pauses | the same, with a note |
| `SessionLimit`, `RateLimited` with a reset | sleep until the reset (5.2) | the same |
| `RateLimited`, no reset | backoff 60 s doubling to 30 min | the same |
| `AuthFailed` | pause: "Run `claude login` (or re-enroll the pool account), then `agent-bridge run`" (L18) | the same |
| `Unsupported` | exit 2 with the fix | the same |
| `TransientError` | backoff 60 s doubling to 30 min; 10 in a row pauses | the same |
| Empty reply | `TransientError` | one in-session nudge, then `TransientError` |

Every retry, sleep and pause is an event in `events.jsonl` and a line on the console.
Pauses and anything the owner should review also go to `review.log`.

### 6.2 Safety

- **One bridge per project.** A `fcntl.flock` on `.bridge/lock`, recording pid, start
  time and argv. It is released by the OS if the process dies (C2).
- **STOP.** `agent-bridge stop` writes `.bridge/STOP`. It is checked at every step
  boundary and every 5 s during sleeps and waits; the running turn finishes, and the
  pending message is saved to `unsent_reply.md` on every path (L13). `stop --now` also
  aborts the running turn and saves the message with a resume note. A new `run` clears
  STOP only after taking the lock.
- **Read-only, reported per role.** `check`, `status` and the start banner print it,
  for example "supervisor: claude-code, read-only enforced (--tools Read,Grep,Glob)" or
  "supervisor: opencode, read-only by instruction; tripwire: repo delta and tool-call
  audit". The tripwire logs which paths changed during the turn, not just "something
  changed" (L10).
- **Tool-call audit, from events.** The builder's shell commands are matched against
  danger patterns (force push, `push` when `git.push: never`, `reset --hard`,
  `rm -rf` outside the repo, `DROP TABLE`, plus `safety.danger_commands`), and hits go
  to `review.log` with the exact command. Prose is not matched (L9).
- **Git.** The bridge never commits, configures git or pushes. After each builder turn
  it lists new commits and flags any whose message carries `Co-Authored-By` or AI
  attribution, or whose author differs from the repo's local `user.email`. It flags
  remote-tracking refs that moved when `git.push: never`. The rules template forbids
  co-author lines and force-push.
- **Sessions bound to the repo.** `pin` and startup check that an opencode session's
  directory (read-only DB lookup) or a claude session file's cwd is this repo or the
  role's neutral directory (L17). There is no `--continue` fallback (C3).
- **Environment.** `ANTHROPIC_API_KEY` is stripped for every child unless
  `billing: api-key`. `project.env_file` (opt-in) loads variables into the builder's
  environment only. For an opencode builder that means the server's environment, so
  the server must be restarted for changes to apply; `status` warns when the file has
  changed since the server started.
- **caffeinate.** On macOS, `run` starts `caffeinate -is -w <own pid>`, which exits
  with the bridge.

### 6.3 Prompts

- Generic text is built in: the shared parts of the three copies' `SYSTEM` and
  `AUTONOMOUS`, the read-only and absolute-path rules, re-verify, the output grammar,
  the autonomous rules and the completion rule.
- Project text comes from config and repo docs:
  - `project.supervisor_rules` (for example `docs/SUPERVISOR.md`, the project
    principles F, X and N hard-coded), inlined into the supervisor's first message;
  - `owner_only` (the work only the owner may do);
  - `project.phases` (where the phase plan lives, for example "PRD section 14");
  - the doc paths.
- **Repeat detection** no longer injects task text into the builder. It feeds the idle
  backstop in 5.1, and its notes go to the supervisor, labelled `[bridge]` (L12).
- **Templates.** `init` and `new` write a "Running under agent-bridge" block for
  `CLAUDE.md`: headless (no question tools), WAIT lines, the report format, the commit
  rules, and long jobs run as background processes with their pid and log recorded in
  the worklog.

## 7. Configuration: `bridge.yaml`

Looked up in the repo root, or passed with `--config PATH`. Unknown keys are errors,
so a typo cannot silently fall back to a default. Durations accept `30m`, `3h`, `1d`
or seconds. Paths are relative to the repo and stored absolute.

```yaml
version: 1
project:
  name: FillingQA
  repo: .                          # relative to this file
  spec: docs/PRD.md
  phases: "PRD section 14"         # where the spec's phase plan is
  rules: [CLAUDE.md]               # the builder's working agreement
  supervisor_rules: docs/SUPERVISOR.md   # optional project principles for the supervisor
  decisions_doc: docs/TRADEOFFS.md       # AUTONOMOUS DECISION entries
  open_items_doc: docs/OPEN.md           # OWNER-BLOCKED items
  worklog: docs/WORKLOG.md               # optional; used for handoffs
  env_file: null                   # opt-in, e.g. .env (builder only)
mode: autonomous                   # autonomous | escalate
owner_only:                        # work the builder must never do
  - hand-written eval items (unanswerable, adversarial, natural phrasing)
builder:
  backend: opencode                # opencode | claude-code
  model: anthropic/claude-opus-5-5 # a bare model id gets "anthropic/" for opencode
  variant: null                    # opencode --variant; claude --effort
  read_only: false
  timeout: 3h
supervisor:
  backend: claude-code
  model: claude-fable-5-1
  variant: xhigh
  read_only: true
  timeout: 30m
opencode:
  port: 4096                       # one per project
  accept: "1.18"
billing: subscription              # subscription | api-key
git:
  push: never                      # never | allowed
waits:
  max: 24h
budget:                            # all optional; reaching one pauses with a summary
  max_exchanges: null              # per `run` invocation
  max_wall_time: null              # per invocation, waits included
  max_unchanged_exchanges: 12      # consecutive, outside waits
rotation:
  builder_max_context_tokens: 600000
  builder_on_phase_complete: false
  supervisor_max_context_tokens: 400000
  supervisor_on_phase_complete: true
safety:
  danger_commands: []              # extra regexes over builder shell commands
  caffeinate: true
```

Fixed internal constants (documented, not configurable): reply cap 24,000 chars,
backoff 60 s to 30 min, 3 timeouts or 3 questions or 10 errors in a row pause, a 15 s
poll interval, a 2-minute reset margin.

## 8. State directory: `.bridge/`

Compatible with the old layout, so migration keeps the logs.

| File | Content |
|---|---|
| `state.json` | the machine state (section 5); `schema: 1` |
| `lock` | flock plus pid, start time, argv |
| `events.jsonl` | structured events: the source for `status` and `logs -f` |
| `loop.log` | human transcript, in the old `EXCHANGE n` format, appended |
| `review.log` | owner-facing warnings, in the old `=== … ===` format, appended |
| `console.log` | everything printed, appended, with a launch marker per run (L16) |
| `serve.log` | the opencode server's output, appended |
| `sessions.json` | every session the bridge created or adopted: role, backend, id, title, directory, created, retired |
| `turns/NNNN-builder.jsonl`, `turns/NNNN-supervisor.jsonl` | raw event streams; the 200 most recent kept |
| `builder_last.md` | the last builder report (kept for compatibility) |
| `inbox/` | owner messages from `say`, not yet delivered |
| `STOP`, `STOP_NOW` | stop requests |
| `unsent_reply.md` | the pending message saved on stop |
| `PAUSED.md` | the pause summary |
| `owner_todo.md` | output of `review` |

`init` and `new` add `.bridge/` to `.gitignore`.

## 9. CLI

Every command takes `--repo PATH` (default: the cwd's git root) and `--config PATH`.

| Command | What it does |
|---|---|
| `init` | Write a commented `bridge.yaml` (detecting existing docs, picking a free opencode port), add `.bridge/` to `.gitignore`, suggest the `AGENTS.md` symlink. Never overwrites. |
| `new "build X" [--auto-approve]` | Create or use the repo; write `bridge.yaml`. A supervisor drafting turn returns the brief (spec with phases, exit criteria and owner-only work), `CLAUDE.md`, `docs/SUPERVISOR.md` and doc skeletons as FILE blocks, which the **bridge** writes, so the supervisor stays read-only. Shows them for approval (approve, revise with feedback, quit) unless `--auto-approve`; without a TTY it stops after drafting. Then it kicks off and runs `--forever`. |
| `run [--forever \| --loop N] [--kickoff MSG\|@file] [--new-supervisor] [--new-builder] [--approve] [--background]` | Run the loop. With neither `--forever` nor `--loop`, one exchange. `--approve` shows each supervisor reply for send / edit / discard (the old manual mode). `--background` detaches, writes to `console.log` and prints the pid. |
| `status [--json]` | Running, sleeping, waiting or paused (with the reason); current exchange and turn age; last verdict; active wait or sleep and when it ends; sessions with context size; read-only kind per role; warnings since launch; budget use; pending owner messages; open DECISIONS NEEDED. |
| `stop [--now]` | Section 6.2. |
| `pin --builder ID \| --supervisor ID \| --new-builder \| --new-supervisor` | Adopt or reset a session. It checks the session belongs to this repo and refuses while a bridge is running. |
| `say "message" \| @file [--to both\|builder\|supervisor]` | Queue an owner message for the next exchange (it interrupts a wait). By default both agents get it verbatim, labelled as the owner's. |
| `review [--template PATH]` | Build the owner to-do from the decisions doc (AUTONOMOUS DECISION headings, skipping ones with an owner-review line) and the open-items doc (OWNER-BLOCKED headings, table rows and status lines, skipping RESOLVED). It handles all three projects' formats. Writes `.bridge/owner_todo.md` and prints it. `--template` writes an `OWNER_REVIEW.md` skeleton: D1..Dn and a "Done when" checklist. |
| `apply-decisions DOC [--loop N]` | Check that DOC has D-items and a "Done when" list, start a fresh supervisor, send the owner kickoff (adapted from FillingQA's `owner_review_kickoff.md`: commit DOC first, work D1..Dn in order, report hashes and run ids, done when the checklist holds), and run `--forever`. |
| `logs [-f] [--transcript \| --review \| --console \| --serve \| --events] [-n N]` | Default: rendered events. `-f` follows. |
| `check` | Config, binaries and versions, `autoupdate`, auth (`claude auth status`), server port, read-only kind per role, git identity, `.gitignore`, `AGENTS.md`, presence of the configured docs. No model calls. |

Exit codes: 0 complete or stopped; 2 configuration or unsupported; 3 paused; 130
interrupted.

## 10. Live view

The foreground `run` and `logs -f` share one renderer over `events.jsonl`, in plain
ASCII:

```
[22:20:29] == exchange 3 == builder (opencode anthropic/claude-opus-5-5, ses_f08e8379)
[22:20:31]   builder > bash: git status --porcelain
[22:20:40]   builder > edit: scripts/eval_run.py
[22:24:10]   builder | I'm making `--report` stdout-only by extracting a function ...
[22:31:02]   builder + done in 10m33s, ctx 120,541 tokens, 2 new commits
[22:31:03]   supervisor (claude-code claude-fable-5-1, read-only enforced)
[22:31:20]   supervisor > Read ~/FillingQA/docs/WORKLOG.md
[22:32:40]   VERDICT: Builder committed F-144 (65c442f) ... Sound.
[22:32:40]   WAIT FOR PID 64410 (max 24h): sleeping, no model calls
```

Text lines are clipped in the live view; the full text is in `loop.log` and the turn
files. For opencode, the start banner also prints the `opencode attach` command, for
watching the builder inside opencode itself.

## 11. Budget

Checked at every step boundary. When a cap is reached (exchanges or wall time for this
invocation, or the unchanged streak outside waits), the run pauses: `PAUSED.md`
records which cap, the counters, the last verdict, the open decisions and the command
to continue. It never continues silently.

## 12. Testing

- pytest; every test gets a temporary repo (`git init` in `tmp_path`) and a temporary
  `.bridge/` (L8). No test makes a model call or reads the real `~/.local/share/opencode`.
- A fake clock replaces sleeping, so waits, limits and backoff run instantly.
- **Unit:** the output grammar (every directive, sentinel placements, the no-REPLY
  fallback); reset parsing (every format quoted in INVENTORY L2); wait conditions,
  including pid reuse; config validation; message composition and labels; `review`
  extraction over fixtures in all three projects' formats.
- **Engine with FakeBackend:** kickoff to completion; STOP saving the pending message on
  every path; WAIT for each form, wake notes and interruptions; limit sleep and resend;
  auth pause and exit code; `Unsupported` exits without retrying; timeout and question
  bounds; budget pause and summary; rotation, decided once (C1); a fresh supervisor
  after COMPLETE; an owner `say` reaching both agents once; crash recovery from
  `in_flight`; a second instance refused by the lock.
- **Backend plumbing:** command lines for each role and backend; event parsing from
  recorded fixtures; timeout leading to abort; question polling, reject and abort; the
  retry status leading to `SessionLimit` (fake `opencode` and `claude` executables, and
  a fake HTTP server). The fixtures are recorded during the live smoke tests and
  scrubbed of personal data.

## 13. Dependencies

- Runtime: **PyYAML**. `bridge.yaml` is YAML by requirement, the standard library has no
  YAML parser, and a hand-written subset parser for the file that controls unattended
  runs is a correctness risk. `yaml.safe_load` only.
- Development: **pytest**.
- Everything else is stdlib: `subprocess`, `threading`, `urllib`, `sqlite3`,
  `zoneinfo`, `fcntl`, `argparse`, `json`.
- Packaged with `pyproject.toml` (setuptools), `requires-python >= 3.12`, console
  script `agent-bridge`. Installed with
  `uv tool install --editable ~/agent-bridge`, or with `pip` under
  `/opt/homebrew/bin/python3.12`. The `python3` on PATH is 3.9.

## 14. Migration plan (detail goes in `docs/MIGRATION.md`; not performed)

Per project, while its bridge is stopped:

1. **Config.** Write `bridge.yaml`: same port (4096, 4097, 4098), same models, builder
   on opencode. The supervisor stays on opencode at first (the current behaviour, and
   it keeps the supervisor session) or moves to claude-code for enforced read-only
   (this starts a fresh supervisor).
2. **Prompts.** Move the project parts of `SYSTEM` and `AUTONOMOUS` verbatim into
   `docs/SUPERVISOR.md` and `owner_only:`. Keep the repo's `CLAUDE.md` and add the
   agent-bridge block.
3. **State.** `agent-bridge pin --builder $(cat .bridge/session)`, and `--supervisor`
   the same way. Keep `loop.log`, `review.log`, `console.log` and `serve.log`; the
   bridge appends. `unsent_reply.md` and `builder_last.md` become the pending state.
   Leave the kickoff files where they are.
4. **Environment.** X's `.env` becomes `project.env_file: .env`. N's question polling
   and abort are built in. X's `opencode.json` question deny can stay.
5. **Retire.** `tools/bridge.py` stays in each repo until the owner removes it; nothing
   is deleted by the migration.

The pinned TUI-created builder sessions lack opencode's question deny (L5). The
question polling covers them, and `--new-builder` replaces them with a session that has
the deny.

## 15. Build order

Small commits, each with tests:

1. Package skeleton, config, statedir and journal.
2. Protocol, limits and waits.
3. Backend base, FakeBackend and the engine core (turns, STOP, completion).
4. Engine: waits, limits, pauses, budget, rotation, `say`, crash recovery.
5. ClaudeCodeBackend.
6. OpencodeBackend and server management.
7. CLI commands and the live view.
8. `review`, `apply-decisions`, `new`.
9. Live smoke tests (Haiku, temporary repos under `/tmp`), deleting only the created
   sessions by exact id, then fixtures.
10. `docs/MIGRATION.md` and the README.
