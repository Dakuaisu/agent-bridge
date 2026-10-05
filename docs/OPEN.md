# Open items

Status: OPEN, OWNER-BLOCKED or RESOLVED.

## OPEN-001 The `claude` CLI login on this machine had expired: OWNER-BLOCKED
- Status: OWNER-BLOCKED
- Found: 2026-10-06, while checking which Opus id Claude Code serves.
- Evidence:
  - `claude auth status` reported `loggedIn: true`.
  - The first call (`claude -p --model claude-opus-5-5 … "Reply with exactly: PONG"`,
    with `--no-session-persistence`) returned `is_error: true`,
    `terminal_reason: "api_error"`, and "Failed to authenticate: OAuth session expired
    and could not be refreshed".
  - `auth status` then reported `loggedIn: false`.

  This is the shared-login problem from INVENTORY L18.
- Effect: the live Claude Code smoke test cannot run until the owner runs
  `claude login`. Per the overnight instructions, no re-login is attempted.
- Next: the owner runs `claude login`, then the Claude Code smoke test
  (MORNING_REPORT.md says how).

## OPEN-002 Smoke-test commit in ~/src/opencode-claude-bridge: repaired, owner to confirm
- Status: OPEN
- What: the opencode smoke test's builder committed `22af7c8` "Contract: wordcount
  module with pytest tests" on `fix/ec2-audit-hardening`, and wrote two files there (L22).
  It was never pushed.
- Done so far:
  - the branch is back at `f6cb717`, the owner's last commit;
  - your uncommitted `src/index.ts` and `src/index.test.ts` changes are untouched;
  - the commit is kept on the branch `agent-bridge-smoke-accident-20261006`;
  - the files are in `/tmp/agent-bridge-smoke-accident-backup-20261006/`.
- Next (owner):
  1. Check `git -C ~/src/opencode-claude-bridge log --oneline -3` and `git status`.
  2. Then `git -C ~/src/opencode-claude-bridge branch -D agent-bridge-smoke-accident-20261006`
     and `rm -rf /tmp/agent-bridge-smoke-accident-backup-20261006`.
  3. To undo the repair instead: `git reset --soft agent-bridge-smoke-accident-20261006`.

## OPEN-003 Verify the opencode builder's repo rule live
- Status: OWNER-BLOCKED
- What: the fix for L22 (the builder working in another repo) is tested only with fakes:
  - the absolute-path rule in every builder message;
  - the outside-repo pause.
- Why it waits: a live re-run of `scripts/smoke.sh opencode` could again make the
  builder treat `~/src/opencode-claude-bridge`, which holds uncommitted work, as its
  working directory. It needs the owner present.
- Next (owner): commit or stash the work in `~/src/opencode-claude-bridge`, run
  `scripts/smoke.sh opencode`, then check that repo with `git status`.
