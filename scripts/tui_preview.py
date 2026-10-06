"""Render the terminal UI's screens to HTML pictures, without a terminal or any model call.

Usage: .venv/bin/python scripts/tui_preview.py [OUTDIR] [COLSxROWS]
The demo projects are built in a temporary folder with the real state and journal APIs (tests/tui_scenes.py).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))


class NoProcess:
    def __init__(self, *args: object, **kw: object) -> None:
        self.args = args

    def poll(self) -> int | None:
        return None


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/agent-bridge-tui-preview")
    cols, rows = (int(v) for v in (sys.argv[2] if len(sys.argv) > 2 else "150x44").split("x"))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
        import tui_scenes as S

        from agent_bridge.tui.app import App
        from agent_bridge.tui.jobs import Runner

        root = Path(tmp) / "src"
        repo, lock = S.running(root)
        interview = S.interview(root)
        review = S.plan_review(root)
        fresh = S.blank(root)

        def shot(name: str, app: App, setup=None) -> None:  # type: ignore[no-untyped-def]
            app.tick()
            if setup:
                setup(app)
                app.tick()
            cv = app.render(rows, cols)
            (out / f"{name}.html").write_text(cv.to_html(name))
            (out / f"{name}.txt").write_text("\n".join(cv.text()) + "\n")
            print(out / f"{name}.html")

        def make(folder: Path) -> App:
            return App(folder, splash=False, runner=Runner(popen=NoProcess))

        shot("01-dashboard", make(repo))
        shot("02-message", make(repo), lambda a: a.act_message("both"))
        shot("03-palette", make(repo), lambda a: a.act_palette())
        shot("04-changes", make(repo), lambda a: a.act_approve())
        shot("05-plan-viewer", make(repo), lambda a: a.act_plan())
        shot("06-projects", make(repo), lambda a: a.toggle_projects())
        shot("07-interview", make(interview), lambda a: a.act_answer())
        shot("08-plan-review", make(review))
        shot("09-approve-plan", make(review), lambda a: a.act_approve())
        shot("10-welcome", make(fresh))
        shot("11-new-project", make(fresh), lambda a: a.act_new())
        shot("12-run", make(review.parent / "ledger-sync"), lambda a: None)
        splash = App(repo, splash=True, runner=Runner(popen=NoProcess))
        splash.splash_start = time.monotonic() - 0.6
        splash.splash_until = time.monotonic() + 10
        shot("13-splash", splash)
        if lock:
            lock.release()
        shot("14-stopped-run-form", make(repo), lambda a: a.act_run())
        shot("15-help", make(repo), lambda a: a.act_help())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
