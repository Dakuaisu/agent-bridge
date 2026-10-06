"""A scripted backend: the only one the test suite talks to (docs/DESIGN.md 4.4)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Union

from agent_bridge.backends.base import (
    READ_ONLY_ENFORCED,
    Backend,
    BackendError,
    CancelToken,
    Cancelled,
    Capabilities,
    Event,
    Health,
    Reply,
    ToolCall,
)
from agent_bridge.clock import Clock
from agent_bridge.config import RoleConfig


@dataclass
class FakeStep:
    text: str = ""
    error: BackendError | None = None
    served_model: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    context_tokens: int | None = None
    advance: float = 0.0
    files: dict[str, str] = field(default_factory=dict)
    commit: str | None = None
    events: list[Event] = field(default_factory=list)
    action: Callable[[], None] | None = None
    cost_usd: float | None = None


Step = Union[FakeStep, str, BackendError]
Script = Union[list[Step], Callable[[str, "FakeBackend"], Step]]


class FakeScriptExhausted(AssertionError):
    pass


class FakeBackend(Backend):
    engine = "fake"

    def __init__(
        self,
        role_cfg: RoleConfig,
        *,
        repo: Path,
        project: str = "test",
        clock: Clock,
        script: Script,
        read_only: str = READ_ONLY_ENFORCED,
    ) -> None:
        super().__init__(role_cfg, repo=repo, project=project)
        self.clock = clock
        self.script = script
        self.read_only_text = read_only
        self.sent: list[str] = []
        self.system_prompts: list[str | None] = []
        self.sessions_started = 0
        self.aborts = 0
        self._step_index = 0

    def version_check(self) -> str:
        return "fake 1.0"

    def health_check(self) -> Health:
        return Health(True, "fake backend")

    def capabilities(self) -> Capabilities:
        return Capabilities(read_only=self.read_only_text, question_guard="scripted")

    def abort(self) -> None:
        self.aborts += 1

    def _next(self, message: str) -> Step:
        if callable(self.script):
            return self.script(message, self)
        if self._step_index >= len(self.script):
            raise FakeScriptExhausted(f"{self.role}: script exhausted after {len(self.script)} steps; last message:\n{message}")
        step = self.script[self._step_index]
        self._step_index += 1
        return step

    def send(
        self,
        message: str,
        *,
        timeout: float,
        on_event: Callable[[Event], None],
        cancel: CancelToken,
        on_session: Callable[[str], None],
        raw_path: Path | None = None,
    ) -> Reply:
        self.sent.append(message)
        self.system_prompts.append(self.system_prompt)
        if cancel.is_set():
            raise Cancelled("stop --now before the turn started")
        if self._session_id is None:
            self.sessions_started += 1
            self._session_id = f"fake-{self.role}-{self.sessions_started}"
        on_session(self._session_id)
        step = self._next(message)
        if isinstance(step, BackendError):
            raise step
        if isinstance(step, str):
            step = FakeStep(text=step)
        for rel, content in step.files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if step.commit:
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", step.commit], cwd=self.repo, check=True, capture_output=True)
        if step.action:
            step.action()
        if step.advance:
            self.clock.sleep(step.advance)
        for event in step.events:
            on_event(event)
        if raw_path is not None:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"fake": True, "text": step.text}) + "\n")
        if step.error is not None:
            raise step.error
        if cancel.is_set():
            raise Cancelled("stop --now during the turn")
        return Reply(
            text=step.text,
            session_id=self._session_id,
            served_models={step.served_model or self.cfg.engine_model().split("/")[-1]},
            tool_calls=list(step.tool_calls),
            context_tokens=step.context_tokens,
            duration_s=step.advance,
            raw_path=raw_path,
            cost_usd=step.cost_usd,
        )
