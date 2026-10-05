from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agent_bridge.backends.base import QuestionAsked, SessionLimit, Timeout, TransientError
from agent_bridge.backends.fake import FakeStep
from agent_bridge.clock import FakeClock
from agent_bridge.engine import State
from agent_bridge.statedir import StateDir

from harness import DONE, events, make_engine, ok, review_log

BRIDGE = ".bridge"


def state(repo: Path) -> State:
    return State.load(repo / BRIDGE / "state.json")


def pid_alive_until(clock: FakeClock, seconds: float):
    start = clock.now()

    def pid_start(pid: int) -> str | None:
        return "Fri Oct  2 22:30:00 2026" if (clock.now() - start).total_seconds() < seconds else None

    return pid_start


# -- WAIT


def test_builder_wait_request_is_honoured_without_model_calls(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["Scheduled the eval resume as pid 4242.\nWAIT FOR PID 4242", "The run finished; committed."],
        supervisor=[ok("When the job exits, check the run and commit it."), DONE],
        pid_start=pid_alive_until(clock, 4 * 3600),
    )
    start = clock.now()
    eng.kickoff("go")
    assert eng.run() == 0
    assert clock.now() - start >= timedelta(hours=4)
    b, s = eng.backends["builder"], eng.backends["supervisor"]
    assert len(b.sent) == 2 and len(s.sent) == 2
    assert "The builder asked for: WAIT FOR PID 4242" in s.sent[0]
    assert "[bridge] The wait (WAIT FOR PID 4242) is over: process 4242 exited." in b.sent[1]
    assert "[supervisor]\nWhen the job exits" in b.sent[1]
    waited = [e for e in events(repo) if e["kind"] in ("wait_start", "wait_end")]
    assert [e["kind"] for e in waited] == ["wait_start", "wait_end"]
    assert waited[0]["source"] == "builder"


def test_no_wait_overrides_the_builders_request(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["Waiting.\nWAIT FOR PID 4242", "did it"],
        supervisor=["VERDICT: no\nSCOPE: R-1\nNO WAIT\nREPLY:\nDo the docs while it runs.", DONE],
        pid_start=lambda pid: "alive",
    )
    eng.kickoff("go")
    eng.run()
    assert not events(repo, "wait_start")
    assert "Do the docs while it runs." in eng.backends["builder"].sent[1]


def test_supervisor_wait_until_sleeps_to_the_time(repo: Path, clock: FakeClock) -> None:
    target = (clock.now() + timedelta(hours=7)).isoformat(timespec="seconds")
    eng = make_engine(
        repo,
        clock,
        builder=["paused until the quota resets", "day 2 numbers"],
        supervisor=[f"VERDICT: wait\nSCOPE: none\nWAIT UNTIL {target}\nREPLY:\nReport the day-2 numbers.", DONE],
    )
    start = clock.now()
    eng.kickoff("go")
    eng.run()
    assert clock.now() - start >= timedelta(hours=7)
    assert f"The wait (WAIT UNTIL {target}) is over" in eng.backends["builder"].sent[1]


def test_wait_for_file_ends_at_max(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["started", "checked"],
        supervisor=["VERDICT: wait\nSCOPE: none\nWAIT FOR FILE results/done.json MAX 1h\nREPLY:\nCheck the job.", DONE],
    )
    eng.kickoff("go")
    eng.run()
    assert "ended: MAX reached" in eng.backends["builder"].sent[1]


def test_wait_longer_than_waits_max_is_clamped(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["started", "checked"],
        supervisor=["VERDICT: wait\nSCOPE: none\nWAIT FOR PID 77 MAX 3d\nREPLY:\nCheck.", DONE],
        pid_start=lambda pid: "alive",
        toml="[waits]\nmax = '2h'\n",
    )
    start = clock.now()
    eng.kickoff("go")
    eng.run()
    assert "WAIT CLAMPED" in review_log(repo)
    assert timedelta(hours=2) <= clock.now() - start < timedelta(hours=3)


def test_owner_message_interrupts_a_wait(repo: Path, clock: FakeClock) -> None:
    sd = StateDir(repo)

    def pid_start(pid: int) -> str | None:
        if not list(sd.inbox.glob("*.json")) and not (sd.inbox / "delivered").exists():
            sd.inbox_put("say", {"text": "Skip the eval; write the docs.", "to": "both"}, "2026-10-06T12:00:00+05:30")
        return "alive"

    eng = make_engine(repo, clock, builder=["WAIT FOR PID 9", "docs written"], supervisor=[ok("wait for it"), DONE], pid_start=pid_start)
    eng.kickoff("go")
    eng.run()
    second = eng.backends["builder"].sent[1]
    assert "[owner] (verbatim; binding)\nSkip the eval; write the docs." in second
    assert "was interrupted by an owner message" in second
    assert "(verbatim; binding; already delivered to the builder)" in eng.backends["supervisor"].sent[1]


def test_stop_during_a_wait_saves_the_reply_and_resumes_the_wait(repo: Path, clock: FakeClock) -> None:
    sd = StateDir(repo)
    alive = {"v": True}

    def pid_start(pid: int) -> str | None:
        sd.request_stop()
        return "alive" if alive["v"] else None

    eng = make_engine(repo, clock, builder=["WAIT FOR PID 9", "done"], supervisor=[ok("Check the run."), DONE], pid_start=pid_start)
    eng.kickoff("go")
    assert eng.run() == 0
    assert state(repo).pause["resume"] == "WAITING"
    assert "[supervisor]\nCheck the run." in (repo / BRIDGE / "unsent_reply.md").read_text()
    alive["v"] = False
    eng.pid_start = lambda pid: None
    assert eng.run() == 0
    assert state(repo).phase == "COMPLETE"
    assert "is over" in eng.backends["builder"].sent[1]


# -- limits and errors


def test_session_limit_sleeps_until_the_reset_and_resends_with_a_note(repo: Path, clock: FakeClock) -> None:
    reset = clock.now() + timedelta(hours=12, minutes=29)
    eng = make_engine(
        repo,
        clock,
        builder=[SessionLimit("Claude pool exhausted — next account free in 12h 29m", reset_at=reset), "worked", "more"],
        supervisor=[ok("go"), DONE],
    )
    eng.kickoff("go")
    eng.run()
    assert clock.now() >= reset + timedelta(minutes=2)
    second = eng.backends["builder"].sent[1]
    assert "cut off at 12:00 by a usage limit" in second
    assert "USAGE LIMIT (builder)" in review_log(repo)
    assert len(eng.backends["builder"].sent) == 3


def test_unknown_reset_backs_off_fifteen_minutes(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[SessionLimit("usage limit reached"), ok("go"), DONE])
    start = clock.now()
    eng.kickoff("go")
    eng.run()
    assert clock.now() - start >= timedelta(minutes=17)


def test_repeated_errors_back_off_then_pause(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[TransientError("exit 1")] * 10, supervisor=[])
    eng.kickoff("go")
    assert eng.run() == 3
    st = state(repo)
    assert st.pause["reason"] == "repeated errors" and st.counters["errors_builder"] == 10
    sleeps = [e for e in events(repo, "sleep_start")]
    assert len(sleeps) == 9


def test_builder_timeouts_resend_one_resume_note_then_pause(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[Timeout("3h"), Timeout("3h"), "finally", Timeout("x"), Timeout("x"), Timeout("x")], supervisor=[ok("go")])
    eng.kickoff("go")
    assert eng.run() == 3
    sent = eng.backends["builder"].sent
    assert sent[1].count("cut off by the bridge's timeout") == 1
    assert sent[2].count("cut off by the bridge's timeout") == 1
    assert "cut off" not in sent[3]
    assert state(repo).pause["reason"] == "repeated timeouts"


def test_question_tool_abort_is_resent_with_the_questions(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[QuestionAsked(["Which deps do you approve? [a, b]"]), "listed under DECISIONS NEEDED", "x"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    second = eng.backends["builder"].sent[1]
    assert "called the interactive question tool" in second and "- Which deps do you approve? [a, b]" in second
    assert "QUESTION TOOL ABORTED (builder, 1 in a row)" in review_log(repo)


def test_stop_now_aborts_and_saves_with_a_resume_note(repo: Path, clock: FakeClock) -> None:
    sd = StateDir(repo)
    eng = make_engine(repo, clock, builder=[FakeStep(text="partial", action=lambda: sd.request_stop(now=True))], supervisor=[])
    eng.kickoff("go")
    assert eng.run() == 0
    unsent = (repo / BRIDGE / "unsent_reply.md").read_text()
    assert "stopped the bridge (stop --now)" in unsent and "[owner] (verbatim; binding)\ngo" in unsent
    assert state(repo).pause["reason"] == "stop"


# -- budget and idle


def test_max_exchanges_pauses_with_a_summary(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r"] * 5, supervisor=[ok(f"step {i}") for i in range(5)], toml="[budget]\nmax_exchanges = 2\n")
    eng.kickoff("go")
    assert eng.run() == 3
    paused = (repo / BRIDGE / "PAUSED.md").read_text()
    assert "# Paused: budget" in paused and "max_exchanges reached: 2" in paused
    assert len(eng.backends["supervisor"].sent) == 2


def test_max_wall_time(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[FakeStep(text="r", advance=3600)] * 5, supervisor=[ok("next")] * 5, toml="[budget]\nmax_wall_time = '2h'\n")
    eng.kickoff("go")
    assert eng.run() == 3
    assert "max_wall_time" in state(repo).pause["detail"]


def test_idle_note_then_backstop_sleep_then_budget_pause(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["no change"] * 20, supervisor=[ok("wait more")] * 20, toml="[budget]\nmax_unchanged_exchanges = 6\n")
    eng.kickoff("go")
    assert eng.run() == 3
    sup = eng.backends["supervisor"].sent
    assert "changed nothing in the repo" not in sup[1]
    assert "The last 3 exchanges changed nothing in the repo" in sup[2]
    assert "IDLE BACKSTOP" in review_log(repo)
    assert "max_unchanged_exchanges reached: 6" in state(repo).pause["detail"]


# -- rotation


def test_phase_complete_rotates_both_sessions_with_handoffs(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(
        repo,
        clock,
        builder=["Phase 1 complete.", "Started phase 2.", "more"],
        supervisor=["VERDICT: verified\nSCOPE: Phase 2\nPHASE COMPLETE: Phase 1\nREPLY:\nStart Phase 2 with R-4.", ok("go"), DONE],
        toml="worklog = 'docs/WORKLOG.md'\n",
    )
    eng.kickoff("go")
    eng.run()
    b, s = eng.backends["builder"], eng.backends["supervisor"]
    assert b.sessions_started == 2 and s.sessions_started == 2
    assert "This is a fresh builder session; the previous one (fake-builder-1) was retired (Phase 1 complete)" in b.sent[1]
    assert "docs/WORKLOG.md" in b.sent[1]
    assert "ROLE FOR THIS ENTIRE SESSION" in s.sent[1] and "fresh supervisor session (Phase 1 complete)" in s.sent[1]
    assert "PHASE COMPLETE: Phase 1 (verified by the supervisor at exchange 1)" in review_log(repo)
    assert state(repo).phases_done[0]["phase"] == "Phase 1"
    registry = json.loads((repo / BRIDGE / "sessions.json").read_text())
    assert [r["retired_reason"] for r in registry if r["retired"]] == ["Phase 1 complete", "Phase 1 complete"]


def test_context_size_rotates_the_builder(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=[FakeStep(text="big", context_tokens=650_000), "fresh", "x"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    assert "opens a fresh session (context reached 650,000 tokens)" in eng.backends["supervisor"].sent[0]
    assert eng.backends["builder"].sessions_started == 2


def test_rotate_builder_directive(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=["VERDICT: v\nSCOPE: R-1\nROTATE BUILDER\nREPLY:\nSelf-contained task.", DONE])
    eng.kickoff("go")
    eng.run()
    assert eng.backends["builder"].sessions_started == 2
    assert "retired (ROTATE BUILDER from the supervisor)" in eng.backends["builder"].sent[1]


# -- owner messages and recovery


def test_say_routing(repo: Path, clock: FakeClock) -> None:
    sd = StateDir(repo)
    eng = make_engine(
        repo,
        clock,
        builder=[FakeStep(text="r0", action=lambda: (sd.inbox_put("say", {"text": "Prefer SQLite.", "to": "both"}, "t1"), sd.inbox_put("say", {"text": "Be strict on tests.", "to": "supervisor"}, "t2"))), "r1"],
        supervisor=[ok("go"), DONE],
    )
    eng.kickoff("go")
    eng.run()
    s0, b1 = eng.backends["supervisor"].sent[0], eng.backends["builder"].sent[1]
    assert "[owner] (verbatim; binding; delivered to the builder with your REPLY)\nPrefer SQLite." in s0
    assert "[owner] (verbatim; binding; for you)\nBe strict on tests." in s0
    assert "[owner] (verbatim; binding)\nPrefer SQLite." in b1 and "Be strict" not in b1
    assert "Prefer SQLite." not in eng.backends["supervisor"].sent[1]


def test_say_after_completion_starts_new_work(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=[ok("go"), DONE, DONE])
    eng.kickoff("go")
    eng.run()
    StateDir(repo).inbox_put("say", {"text": "One more thing: add a README badge.", "to": "both"}, "t")
    eng.run()
    assert "One more thing" in eng.backends["builder"].sent[2]
    assert eng.backends["supervisor"].sessions_started == 2


def test_crash_mid_turn_resends_with_a_restart_note(repo: Path, clock: FakeClock) -> None:
    class Crash(Exception):
        pass

    def crash() -> None:
        raise Crash("power cut")

    eng = make_engine(repo, clock, builder=[FakeStep(text="never seen", action=crash)], supervisor=[])
    eng.kickoff("go")
    with pytest.raises(Crash):
        eng.run()
    assert state(repo).pending["in_flight"] is True
    eng2 = make_engine(repo, clock, builder=["recovered"], supervisor=[DONE])
    eng2.run()
    assert "The bridge restarted while you were working" in eng2.backends["builder"].sent[0]
    assert eng2.backends["builder"].aborts == 1
    assert eng2.backends["builder"].sent[0].count("[owner] (verbatim; binding)\ngo") == 1


# -- the builder must stay in the repo (INVENTORY L22: the smoke-test incident)


def test_builder_messages_name_the_repo_and_require_absolute_paths(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    root = eng.cfg.project.repo
    for message in eng.backends["builder"].sent:
        assert f"[bridge] The repository is {root}. Use absolute paths under it" in message
        assert f"start every shell command with `cd {root} &&`" in message
    assert "The repository is" not in eng.backends["supervisor"].sent[1].split("===== BUILDER REPORT")[0].split("builder was told")[-1]


def test_builder_writing_in_another_repo_pauses_the_run(repo: Path, clock: FakeClock) -> None:
    from agent_bridge.backends.base import ToolCall

    calls = [
        ToolCall("write", "/Users/someone/src/other-project/docs/PRD.md"),
        ToolCall("bash", "cd /Users/someone/src/other-project && git add docs/PRD.md && git commit -m x"),
        ToolCall("bash", f"cd {repo} && pytest -q"),
        ToolCall("bash", "/opt/homebrew/bin/python3.12 -m pytest"),
    ]
    eng = make_engine(repo, clock, builder=[FakeStep(text="committed", tool_calls=calls)], supervisor=[])
    eng.kickoff("go")
    assert eng.run() == 3
    st = state(repo)
    assert st.pause["reason"] == "outside the repo" and st.pause["resume"] == "SUPERVISOR_TURN"
    assert "/Users/someone/src/other-project/docs/PRD.md" in st.pause["detail"]
    log = review_log(repo)
    assert "BUILDER WROTE OUTSIDE THE REPO at exchange 0: run paused" in log
    assert "pytest" not in log.split("run paused")[1].split("===")[0]


def test_builder_reading_elsewhere_is_only_reported(repo: Path, clock: FakeClock) -> None:
    from agent_bridge.backends.base import ToolCall

    calls = [ToolCall("read", "/Users/someone/notes.md"), ToolCall("bash", "ls -la /Users/someone/src/other-project/docs/")]
    eng = make_engine(repo, clock, builder=[FakeStep(text="looked around", tool_calls=calls), "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    assert eng.run() == 0
    assert "BUILDER READ OUTSIDE THE REPO at exchange 0" in review_log(repo)


def test_an_adopted_supervisor_session_gets_the_role_once(repo: Path, clock: FakeClock) -> None:
    from agent_bridge.statedir import atomic_write_json

    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=[ok("go"), ok("again"), DONE])
    eng.st.sessions["supervisor"] = {"engine": "fake", "id": "ses_old", "closed": False, "adopted": True, "needs_role": True}
    eng.save()
    eng = make_engine(repo, clock, builder=["r0", "r1", "r2"], supervisor=[ok("go"), ok("again"), DONE])
    eng.kickoff("go")
    eng.run()
    sent = eng.backends["supervisor"].sent
    assert eng.backends["supervisor"].sessions_started == 0
    assert "ROLE FOR THIS ENTIRE SESSION" in sent[0]
    assert "ROLE FOR THIS ENTIRE SESSION" not in sent[1] and "Reminder:" in sent[1]


def test_a_role_moved_to_another_engine_starts_a_fresh_session(repo: Path, clock: FakeClock) -> None:
    eng = make_engine(repo, clock, builder=["r0"], supervisor=[ok("go")])
    eng.st.sessions["supervisor"] = {"engine": "opencode", "id": "ses_old", "closed": False, "adopted": True, "needs_role": True}
    eng.save()
    eng = make_engine(repo, clock, builder=["r0", "r1"], supervisor=[ok("go"), DONE])
    eng.kickoff("go")
    eng.run()
    sup = eng.backends["supervisor"]
    assert sup.sessions_started == 1 and "ROLE FOR THIS ENTIRE SESSION" in sup.sent[0]
    assert eng.st.sessions["supervisor"]["id"] != "ses_old"
