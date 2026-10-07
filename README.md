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

The full command reference, for the terminal UI and without it, is in
[docs/USAGE.md](docs/USAGE.md).

## Engines

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
  repo, the temp folders and the tools' own state (`safety.sandbox`). That was checked
  live with one Haiku call in a temporary repo; no real project's builder has run under
  it yet. An opencode builder is not sandboxed (OPEN-004). There, a write outside the
  repo is caught after the turn and pauses the run. That covers absolute paths, `~`,
  `$HOME`, `cd` and `git -C`. A relative path that a tool resolved in another folder
  shows up only as "the builder wrote, but the repo did not change" (INVENTORY L22,
  OPEN-003).
- **Commit trailers are detected, not prevented.** In the final smoke test a Haiku
  builder added `Co-Authored-By` despite the rules; the bridge logged `COMMIT
  ATTRIBUTION`. Nothing rewrites history.
- **Phases must be `Phase N` headings** for the bridge to track them (current phase,
  the all-phases-verified hint). Phase plans in tables work for the supervisor, but
  without those two aids.
- **opencode 2.x is not supported.** The flags this uses were removed there.
- **Shared logins.** If the `claude` login expires mid-run, the bridge pauses with "run
  `claude login`". It never logs in for you.
- **Live use is still small.**
  - Haiku smoke tests in temporary repos: three on Claude Code (the last reached PROJECT
    COMPLETE) and one on opencode.
  - One real project: FilingQA moved on 2026-10-06 with the default Claude Code models
    (`docs/MIGRATION.md`, section 6). Its first run lasted about 8 minutes (two builder
    turns on `claude-opus-5-5`, one supervisor turn on `claude-fable-5-1`) before it was
    stopped by hand.
  - Not yet seen on a real project: a planner turn, PHASE COMPLETE, builder rotation,
    PROJECT COMPLETE, sleeps for WAITs or usage limits, `decide`, an opencode role, and
    what was added after that run (the builder sandbox, `project.verify`, notifications,
    cost tracking).
- **Migration:** FilingQA has moved; xbrl-frontier and netcode-testbed have not.
- **The terminal UI is keyboard-only.** There is no mouse support, so your terminal's own
  text selection keeps working. Inside tmux or screen with a ctrl-a prefix, press it
  twice to send ctrl-a, or use `:` → All projects.
- **The UI has been used on one real project.** Its command log shows init, check,
  approve, run and stop on the FilingQA move. Its other commands were tested only in a
  pseudo-terminal and with rendered previews.
