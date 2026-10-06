# agent-bridge

Runs three agents against one git repo, unattended, with an honest audit trail:

- **planner**: interviews you, writes the contract (`docs/PRD.md`, `CLAUDE.md`, the
  decision ledger, `docs/OPEN.md`), and answers re-plan requests and your decisions;
- **supervisor**: read-only; verifies each builder report against the repo and writes
  the next instruction;
- **builder**: does the work and commits it.

Each role runs on **Claude Code** (`claude -p`) or **opencode 1.18.x**, with its own
model. Python 3.12, standard library only. The design is in `docs/DESIGN.md`, the
decisions taken while building it are in `docs/DECISIONS.md`, and open items are in
`docs/OPEN.md`.

## Install

Requirements:
- Python 3.12 or newer;
- the `claude` CLI, logged in (`claude auth status`), for any role on Claude Code;
- opencode **1.18.x** (tested with 1.18.30), for any role on opencode, with
  `"autoupdate": false`. 2.x is refused.

```
git clone <this repo> agent-bridge && cd agent-bridge
python3.12 -m venv .venv
.venv/bin/pip install -e .            # no runtime dependencies
.venv/bin/agent-bridge --version
```

To run it from any folder, link it onto your PATH (here `~/.local/bin`):

```
ln -s ~/agent-bridge/.venv/bin/agent-bridge ~/.local/bin/agent-bridge
```

Development: `.venv/bin/pip install -e '.[dev]'`, then `.venv/bin/python -m pytest`.

## Run it: one command

```
agent-bridge
```

In a terminal, that opens the terminal UI on the current folder's project:

- **No project here yet.** A start screen offers **New project**: the planner interviews
  you, drafts the plan, and the agents build it. For a repo that already has
  `docs/PRD.md` and `CLAUDE.md`, it offers **Set up this repo** (init, check, adopt).
- **A project.** The dashboard shows:
  - the three agents: engine, model, how read-only is enforced, what each is doing right
    now, and how full its context is;
  - the live stream of the run;
  - what waits for you, the phases, and the alerts.

  The line above the stream always says what to do next.
- **ctrl-a** lists every project agent-bridge has opened on this machine, with its state.
  Enter opens one.

| Key | Action |
|---|---|
| `r` | run, or resume, the loop |
| `s` | stop, after the current step or now |
| `m` | message the agents, verbatim, as `[owner]` |
| `i` | answer the planner's questions |
| `a` | approve the plan, a plan change, or the contract |
| `v` | read the plan files; `e` edits them in `$EDITOR` |
| `d` | apply an owner decisions doc |
| `o` | owner to-do |
| `p` | report |
| `l` | logs |
| `:` | every command |
| `?` | help |
| `q` | quit |

Quitting the UI never stops the bridge. Each action runs an `agent-bridge` command in
the background, and its output is kept under `~/.local/state/agent-bridge/ui/`. Run
`agent-bridge` again to come back. `agent-bridge ui --all` opens on the project list.

The UI needs a terminal of at least 60×16 and looks best with 256 colours. Set
`AGENT_BRIDGE_NO_SPLASH=1` to skip the start animation.

## Without the UI

Every UI action is also a subcommand, for scripts and for terminals without the UI.

### A new project

```
mkdir ~/src/myproject && cd ~/src/myproject && git init
git config user.name "Your Name" && git config user.email "you@example.com"
agent-bridge new "A Python CLI that ..."     # the planner asks a few questions
agent-bridge say @answers.md                 # only if you ran `new` without a terminal
agent-bridge approve                         # read docs/PRD.md and CLAUDE.md first
```

In a terminal, `new` asks the questions inline and asks for approval. `approve` then
runs until PROJECT COMPLETE or a pause. `new --auto-approve --background` plans and builds
on the planner's recommendations without you; every choice it makes is logged as an
AUTONOMOUS DECISION for review.

### An existing repo with a PRD and a CLAUDE.md

```
cd ~/src/existing
agent-bridge init            # writes bridge.toml; never overwrites it
agent-bridge check           # no model calls
agent-bridge approve --no-run
agent-bridge run --forever --kickoff "Start with ..."
```

### Choosing engines

Every role defaults to Claude Code: planner `claude-fable-5-1` at `max` effort,
supervisor `claude-fable-5-1` at `xhigh`, builder `claude-opus-5-5`. Override per role
with `ENGINE:MODEL` on `init` or `new`. These three combinations were set up with `init`
and passed `check` on this machine:

| Combination | Flags | Supervisor read-only |
|---|---|---|
| all Claude Code (default) | none | enforced: `--tools Read,Grep,Glob` |
| Claude Code planner and supervisor, opencode builder | `--builder opencode:anthropic/claude-opus-5-5` | enforced |
| all opencode | `--planner opencode:anthropic/claude-fable-5-1 --supervisor opencode:anthropic/claude-fable-5-1 --builder opencode:anthropic/claude-opus-5-5` | **read-only by instruction only** |

- With any opencode role, `init` picks a free port for `[opencode] port`, and adds `.omo/`
  to `.gitignore`. The bridge starts `opencode serve` on that port, or uses one already
  running there.
- On opencode, read-only cannot be enforced. `status` says "read-only by instruction
  only"; any write or shell call by the supervisor or planner, and any repo change during
  its turn, goes to `.bridge/review.log`.
- A role on a non-default model gets no effort setting unless you set `variant` in
  `bridge.toml`.

### Day to day

| Command | What it does |
|---|---|
| `status` | state, sessions, read-only per role, warnings |
| `logs -f` | follow the live event stream |
| `say "..."` | an owner message, given verbatim to both agents at the next exchange |
| `stop` / `stop --now` | stop at the next step boundary / abort the running turn too |
| `review` | your to-do: autonomous decisions, OWNER-BLOCKED items, waiting plan changes |
| `decide docs/OWNER_REVIEW.md` | apply your decisions through the planner, then continue |
| `report` | the owner-review report (also written at PROJECT COMPLETE) |
| `pin --builder ID` / `pin --new-builder` | adopt or reset a session |

Everything lives in `.bridge/`: `loop.log` holds every message as delivered,
`review.log` holds the warnings, and `state.json` lets a stopped run resume where it
left off.

## Evidence, alerts and limits for unattended runs

Optional settings in `bridge.toml`; each is in the template that `init` writes:

- `[project] verify = "make test"`: the bridge runs it after every builder turn and shows
  the supervisor the exit code and the last lines. Its "tests pass" then rests on a run
  the agents did not report themselves. Owner-only: a plan change cannot set it.
- `[notify]`: a macOS notification (on by default) and/or your own `command`, when a run
  pauses, completes, or needs you. The command gets `AGENT_BRIDGE_EVENT`, `_TITLE`,
  `_MESSAGE`, `_PROJECT` and `_REPO`. For example, curl to ntfy.sh for your phone.
- `[budget] max_cost_usd`: pauses a run once its turns add up to this much at the engines'
  API prices. `status` and the report show the running total.
- `[safety] sandbox`: `auto` (the default) sandboxes a Claude Code builder on macOS;
  `sandbox_writable` adds folders it may write to.

Logs over 64 MB rotate when a run starts (`events.jsonl.1`, …). Ctrl-C, closing the
terminal, or SIGTERM ends the running agent with the bridge, and `agent-bridge run`
resends the interrupted turn.

## Billing

- **Claude Code roles bill your Claude subscription (the Max plan).** By default
  (`billing.mode = "subscription"`) the bridge removes `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` from every child process, and refuses a turn that Claude Code
  reports as using an API key.
- **Third-party harnesses such as opencode may bill differently.** Anthropic can bill
  subscription use through third-party apps as extra usage, depending on your plan and
  login. Check your account before running long jobs on opencode.
- **API-key mode is supported:** set `billing.mode = "api-key"` in `bridge.toml` (an
  owner-only setting; the planner may not change it). The key is then passed through,
  and a Claude Code turn served by the subscription login is refused.

## Known limitations

- **Read-only on opencode is by instruction only.** It is audited, not enforced.
- **Only the Claude Code builder is sandboxed.** On macOS it may write only inside the
  repo, the temp folders and the tools' own state (`safety.sandbox`, verified live). An
  opencode builder is not sandboxed (OPEN-004). There, a write outside the repo is caught
  after the turn and pauses the run. That covers absolute paths, `~`, `$HOME`, `cd` and
  `git -C`. A relative path that a tool resolved in another folder shows up only as "the
  builder wrote, but the repo did not change" (INVENTORY L22, OPEN-003).
- **Commit trailers are detected, not prevented.** In the final smoke test a Haiku
  builder added `Co-Authored-By` despite the rules; the bridge logged `COMMIT
  ATTRIBUTION`. Nothing rewrites history.
- **Phases must be `Phase N` headings** for the bridge to track them (current phase,
  the all-phases-verified hint). Phase plans in tables work for the supervisor, but
  without those two aids.
- **opencode 2.x is not supported.** The flags this uses were removed there.
- **Shared logins.** If the `claude` login expires mid-run, the bridge pauses with "run
  `claude login`". It never logs in for you.
- **Live testing so far was on Haiku only,** in small temporary repos. The default
  Fable and Opus models have not run a full loop under agent-bridge.
- **Migration** of existing projects is documented in `docs/MIGRATION.md` and has not
  been performed.
- **The terminal UI is keyboard-only.** There is no mouse support, so your terminal's own
  text selection keeps working. Inside tmux or screen with a ctrl-a prefix, press it
  twice to send ctrl-a, or use `:` → All projects.
- **The UI was tested in a pseudo-terminal and with rendered previews,** not driven by
  hand against a live run.
