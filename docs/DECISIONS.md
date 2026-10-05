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
