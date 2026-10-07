# Using agent-bridge

The full command reference. Installing, the engines and the safety model are in the
[README](../README.md).

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
| `e` | change engines and models per role |
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

To change the engines of an existing project, press `e` in the UI, or run
`agent-bridge engines --builder opencode:anthropic/claude-opus-5-5`. With no flags,
`agent-bridge engines` shows the current setup. The command:
- rewrites only those roles' lines in `bridge.toml`;
- adds an opencode port when a role needs one;
- re-approves the contract, with a ledger entry;
- starts fresh sessions for the roles whose engine changed.

It refuses while a bridge is running.

The engine combinations, the default models and what each combination enforces are in
the README, under [Engines](../README.md#engines).

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
