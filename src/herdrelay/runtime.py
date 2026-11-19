from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .domain import TargetKey
from .models import HerdrTarget, PendingQuestion, StartedAgent


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    normal_send: bool = True
    native_interrupt: bool = False


@runtime_checkable
class RuntimeClient(Protocol):
    """Backend-neutral discovery, bounded read, and normal prompt submission."""

    capabilities: RuntimeCapabilities

    def list_targets(self) -> list[HerdrTarget]: ...

    def list_panes(self) -> list[HerdrTarget]: ...

    def target_key(self, target: str) -> TargetKey: ...

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget: ...

    def read(
        self,
        target: str,
        *,
        lines: int,
        fmt: str | None = None,
        source: str | None = None,
    ) -> str: ...

    def send(self, target: str, message: str) -> None: ...

    def resolve_target(self, query: str) -> HerdrTarget: ...


@runtime_checkable
class RawInputRuntime(Protocol):
    """Legacy terminal input used only by the Herdr TUI compatibility path."""

    def send_keys(
        self, target: str, keys: Iterable[str], *, interval_seconds: float = 0
    ) -> None: ...

    def send_raw_text(self, target: str, text: str) -> None: ...

    def send_text_enter(self, target: str, text: str) -> None: ...


@runtime_checkable
class QuestionAnswerRuntime(Protocol):
    """Answer one normalized structured question through a backend adapter."""

    def answer_question(
        self, question: PendingQuestion, selected: tuple[int, ...]
    ) -> None: ...


@runtime_checkable
class AgentStartRuntime(Protocol):
    """Legacy ability to create an agent pane from chat."""

    def agent_start(
        self,
        name: str,
        *,
        cwd: str | None = None,
        argv: list[str] | None = None,
        workspace: str | None = None,
        tab: str | None = None,
        split: str | None = None,
        source_pane: str | None = None,
    ) -> StartedAgent: ...


@runtime_checkable
class TargetCloseRuntime(Protocol):
    """Legacy ability to close a terminal target from chat."""

    def pane_close(self, pane_id: str) -> None: ...


@runtime_checkable
class InterruptRuntime(Protocol):
    capabilities: RuntimeCapabilities

    def interrupt(self, target: str) -> dict[str, Any]: ...
