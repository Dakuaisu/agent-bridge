from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_bridge.backends.base import AuthFailed, ToolCall, Unsupported
from agent_bridge.backends.fake import FakeStep
from agent_bridge.clock import FakeClock
from agent_bridge.engine import State

from harness import DONE, events, loop_log, make_engine, ok, review_log


def test_kickoff_to_completion_with_labels(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["Did the first task. Phase 1 complete.", "Fixed the README."],
        supervisor=[ok("Fix the README."), DONE],
    )
    eng.kickoff("Start phase 1.")
    assert eng.run() == 0
    builder, supervisor = eng.backends["builder"], eng.backends["supervisor"]

    first = builder.sent[0]
    assert first.startswith("[bridge] Headless run: never use a question")
    assert "[owner] (verbatim; binding)\nStart phase 1.\n" in first
    assert "[supervisor]" not in first
    assert "[supervisor]\nFix the README.\n" in builder.sent[1]
    assert "[owner]" not in builder.sent[1]

    s1, s2 = supervisor.sent
    assert "ROLE FOR THIS ENTIRE SESSION" in s1 and "Reminder:" not in s1
    assert "[owner] (verbatim; binding; already delivered to the builder)\nStart phase 1." in s1
    assert "===== BUILDER REPORT (exchange 1) =====\nDid the first task. Phase 1 complete.\n===== END =====" in s1
    assert 'The builder says "Phase 1 complete"' in s1
    assert "Reminder: you are the read-only SUPERVISOR of demo" in s2 and "ROLE FOR THIS" not in s2
    assert "Start phase 1." not in s2
    assert supervisor.system_prompts[0] and "SUPERVISOR of demo" in supervisor.system_prompts[0]

    st = State.load(repo / ".bridge/state.json")
    assert st.phase == "COMPLETE" and st.exchange == 2
    assert st.sessions["supervisor"]["closed"] is True
    assert "PROJECT COMPLETE at exchange 2" in review_log(repo)
    log = loop_log(repo)
    assert "EXCHANGE 0 | to builder (fake claude-opus-5-5)" in log
    assert "EXCHANGE 1 | supervisor reply" in log
    assert first.strip() in log


def test_message_events_record_origins(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r1", "r2"], supervisor=[ok("go"), DONE])
    eng.kickoff("hello")
    eng.run()
    msgs = events(repo, "message")
    assert [b["origin"] for b in msgs[0]["blocks"]] == ["bridge", "bridge", "owner"]
    assert [b["origin"] for b in msgs[1]["blocks"]] == ["bridge", "bridge", "supervisor"]
    assert all(m["recipient"] == "builder" for m in msgs)


def test_loop_limit_stops_after_n_exchanges(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=[ok("a"), ok("b")])
    eng.kickoff("go")
    assert eng.run(exchanges=1) == 0
    st = State.load(repo / ".bridge/state.json")
    assert st.phase == "SUPERVISOR_TURN" and st.exchange == 1
    assert len(eng.backends["builder"].sent) == 2 and len(eng.backends["supervisor"].sent) == 1


def test_stop_saves_the_unsent_message_and_resumes(repo: Path, clock: FakeClock) -> None:
    sd_root = repo / ".bridge"
    eng = make_engine(
        repo,
        clock,
        builder=["r0", "after the stop"],
        supervisor=[FakeStep(text=ok("Run the tests."), action=lambda: (sd_root / "STOP").touch()), DONE],
    )
    eng.kickoff("go")
    assert eng.run() == 0
    unsent = (sd_root / "unsent_reply.md").read_text()
    assert "[supervisor]\nRun the tests." in unsent
    st = State.load(sd_root / "state.json")
    assert st.phase == "PAUSED" and st.pause["reason"] == "stop" and st.pause["resume"] == "BUILDER_TURN"
    assert len(eng.backends["builder"].sent) == 1
    assert eng.run() == 0
    assert "[supervisor]\nRun the tests." in eng.backends["builder"].sent[1]
    assert State.load(sd_root / "state.json").phase == "COMPLETE"
    assert not (sd_root / "STOP").exists()


def test_tripwire_names_the_paths_and_write_tools(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["r0", "r1"],
        supervisor=[
            FakeStep(text=ok("go"), files={"docs/notes.md": "edited by the supervisor"}, tool_calls=[ToolCall("Write", "docs/notes.md")]),
            DONE,
        ],
        supervisor_read_only="read-only by instruction only",
    )
    eng.kickoff("go")
    eng.run()
    log = review_log(repo)
    assert "SUPERVISOR TURN CHANGED THE REPO" in log and "docs/notes.md" in log
    assert "SUPERVISOR USED WRITE TOOLS" in log and "Write: docs/notes.md" in log
    assert "read-only by instruction only" in eng.backends["supervisor"].sent[0]


def test_served_model_mismatch_is_logged(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[FakeStep(text=ok("go"), served_model="claude-haiku-4-5-20251001"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "MODEL MISMATCH: supervisor was served by claude-haiku-4-5-20251001, not claude-fable-5-1" in review_log(repo)


def test_danger_commands_and_commit_checks(repo: Path, clock: FakeClock) -> None:
    def foreign_commit() -> None:
        (repo / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=someone@else.invalid", "commit", "-q", "-m", "other author"], cwd=repo, check=True
        )

    eng = make_engine(
        repo,
        clock,
        builder=[
            FakeStep(
                text="pushed",
                tool_calls=[ToolCall("bash", "git push --force origin main"), ToolCall("bash", "pytest -q"), ToolCall("read", "git push")],
                files={"a.txt": "a"},
                commit="feat: a\n\nCo-Authored-By: Some Bot <bot@example.invalid>",
                action=foreign_commit,
            ),
            "r1",
        ],
        supervisor=[ok("go"), DONE],
    )
    eng.kickoff("go")
    eng.run()
    log = review_log(repo)
    assert "DANGER COMMAND (force push)" in log and "git push --force origin main" in log
    assert "DANGER COMMAND (git push while git.push = never)" in log
    assert "pytest" not in log
    assert "COMMIT ATTRIBUTION" in log and "Co-Authored-By:" in log
    assert "COMMIT AUTHOR" in log and "someone@else.invalid" in log


def test_empty_output_gets_one_nudge(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=["", ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    sent = eng.backends["supervisor"].sent
    assert sent[1] == "[bridge] Your previous turn ended without any text. Using only reads, reply now in the required shape: VERDICT, SCOPE, optional directives, REPLY.\n"
    assert "[supervisor]\ngo" in eng.backends["builder"].sent[1]


def test_missing_reply_marker_twice_sends_the_text_with_a_warning(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=["Looks fine to me.", "Still fine.", DONE])
    eng.kickoff("go")
    eng.run()
    assert "no REPLY: line" in eng.backends["supervisor"].sent[1]
    assert "[supervisor]\nStill fine." in eng.backends["builder"].sent[1]
    assert "SUPERVISOR REPLY WITHOUT REPLY:" in review_log(repo)


def test_missing_scope_gets_one_nudge_then_a_warning(repo: Path, clock: FakeClock) -> None:
    no_scope = "VERDICT: ok\nREPLY:\nRun it."
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[no_scope, no_scope, DONE])
    eng.kickoff("go")
    eng.run()
    assert "no SCOPE: line" in eng.backends["supervisor"].sent[1]
    assert "SUPERVISOR REPLY WITHOUT SCOPE" in review_log(repo)
    assert "[supervisor]\nRun it." in eng.backends["builder"].sent[1]


def test_confirm_each_edit_is_labelled_and_discard_keeps_the_review(repo: Path, clock: FakeClock) -> None:
    decisions = iter(["Run the tests, then stop.", None])
    eng = make_engine(
        repo,
        clock,
        builder=["r0", "r1"],
        supervisor=[ok("Run the tests."), ok("Next.")],
        confirm=lambda verdict, reply: next(decisions),
    )
    eng.kickoff("go")
    assert eng.run() == 0
    assert "[supervisor] (edited by the owner)\nRun the tests, then stop." in eng.backends["builder"].sent[1]
    st = State.load(repo / ".bridge/state.json")
    assert st.phase == "SUPERVISOR_TURN" and st.exchange == 2


def test_auth_failure_pauses_with_the_fix(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0"], supervisor=[AuthFailed("Failed to authenticate: OAuth session expired")])
    eng.kickoff("go")
    assert eng.run() == 3
    paused = (repo / ".bridge/PAUSED.md").read_text()
    assert "# Paused: auth" in paused and "claude login" in paused
    assert "PAUSED (auth)" in review_log(repo)


def test_unsupported_stops_without_retrying(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[Unsupported("opencode 2.0.1 is not supported")], supervisor=[])
    eng.kickoff("go")
    assert eng.run() == 2
    assert len(eng.backends["builder"].sent) == 1
    st = State.load(repo / ".bridge/state.json")
    assert st.phase == "BUILDER_TURN" and st.pending is not None
    assert "UNSUPPORTED" in review_log(repo)


def test_session_registry(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    registry = json.loads((repo / ".bridge/sessions.json").read_text())
    assert {(r["role"], r["id"]) for r in registry} == {("builder", "fake-builder-1"), ("supervisor", "fake-supervisor-1")}
    assert all(r["directory"] == str(repo.resolve()) and r["title"].startswith("demo ") for r in registry)


def test_new_work_after_completion_gets_a_fresh_supervisor(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=[ok("go"), DONE, DONE])
    eng.kickoff("first")
    eng.run()
    eng.kickoff("owner review: do D1")
    assert eng.run() == 0
    sup = eng.backends["supervisor"]
    assert sup.sessions_started == 2
    assert "ROLE FOR THIS ENTIRE SESSION" in sup.sent[2]
    assert "fresh supervisor session (new work after PROJECT COMPLETE)" in sup.sent[2]
    assert "D1" in sup.sent[2]


def test_nothing_to_do(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[], supervisor=[])
    assert eng.run() == 2


def test_tool_state_folders_never_trip_the_tripwire(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[FakeStep(text=ok("go"), files={".omo/run-continuation/ses_x.json": "{}"}), DONE])
    eng.kickoff("go")
    eng.run()
    assert "CHANGED THE REPO" not in review_log(repo)
