"""The engine-facing interface every backend implements (docs/DESIGN.md 4.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from agent_bridge.config import RoleConfig

READ_ONLY_ENFORCED = "enforced: --tools Read,Grep,Glob"
READ_ONLY_BY_INSTRUCTION = "read-only by instruction only"

MUTATING_TOOLS = {"write", "edit", "multiedit", "patch", "bash", "notebookedit", "task", "shell", "apply_patch"}


class BackendError(Exception):
    retryable = True

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class RateLimited(BackendError):
    def __init__(self, message: str = "", reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class SessionLimit(BackendError):
    def __init__(self, message: str = "", reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class AuthFailed(BackendError):
    retryable = False


class Timeout(BackendError):
    def __init__(self, message: str = "", partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial


class QuestionAsked(BackendError):
    def __init__(self, questions: list[str]) -> None:
        super().__init__(f"the agent called an interactive question tool: {questions}")
        self.questions = questions


class Cancelled(BackendError):
    """The turn was aborted because the owner asked for `stop --now`."""


class Unsupported(BackendError):
    retryable = False


class TransientError(BackendError):
    pass


@dataclass(frozen=True)
class Capabilities:
    read_only: str
    question_guard: str
    live_attach: str | None = None


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    name: str
    summary: str = ""
    ok: bool = True

    @property
    def mutating(self) -> bool:
        return self.name.lower() in MUTATING_TOOLS


@dataclass(frozen=True)
class Event:
    kind: str  # text | tool | step | status | error
    text: str = ""
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    session_id: str
    served_models: set[str] = field(default_factory=set)
    tool_calls: list[ToolCall] = field(default_factory=list)
    context_tokens: int | None = None
    duration_s: float = 0.0
    raw_path: Path | None = None
    steps: list[str] = field(default_factory=list)


def select_text(reply: Reply, markers: tuple[str, ...]) -> str:
    """The last step that carries a form marker (DESIGN 5.5); a VERDICT from an earlier step is kept."""
    if not reply.steps:
        return reply.text
    chosen = next((i for i in range(len(reply.steps) - 1, -1, -1) if any(m in reply.steps[i] for m in markers)), None)
    if chosen is None:
        return reply.text
    text = reply.steps[chosen]
    if "REPLY:" in markers and "VERDICT:" not in text:
        earlier = [ln for step in reply.steps[:chosen] for ln in step.splitlines() if ln.strip().lstrip("*#- ").startswith("VERDICT:")]
        if earlier:
            text = earlier[-1].strip() + "\n" + text
    return text


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


class NeverCancel:
    def is_set(self) -> bool:
        return False


class Backend(ABC):
    engine = "?"

    def __init__(self, role_cfg: RoleConfig, *, repo: Path, project: str) -> None:
        self.cfg = role_cfg
        self.role = role_cfg.role
        self.repo = repo
        self.project = project
        self._session_id: str | None = None
        self._title = f"{project} {self.role}"
        self.system_prompt: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start_session(self, title: str) -> None:
        self._session_id = None
        self._title = title

    def resume(self, session_id: str) -> None:
        self._session_id = session_id

    def describe(self) -> str:
        return f"{self.engine} {self.cfg.engine_model()}"

    @abstractmethod
    def version_check(self) -> str: ...

    @abstractmethod
    def health_check(self) -> Health: ...

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def send(
        self,
        message: str,
        *,
        timeout: float,
        on_event: Callable[[Event], None],
        cancel: CancelToken,
        on_session: Callable[[str], None],
        raw_path: Path | None = None,
    ) -> Reply: ...

    def abort(self) -> None:
        """Stop the in-flight turn for real (server-side where there is a server)."""

    def session_directory(self, session_id: str) -> str | None:
        """Where this session belongs, for the repo binding check; None if unknown."""
        return None
