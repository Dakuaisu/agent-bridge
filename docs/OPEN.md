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
