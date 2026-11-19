from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .domain import TargetKey
from .models import HerdrTarget, PendingQuestion


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
class QuestionAnswerRuntime(Protocol):
    """Answer one normalized structured question through a backend adapter."""

    def answer_question(
        self, question: PendingQuestion, selected: tuple[int, ...]
    ) -> None: ...


@runtime_checkable
class InterruptRuntime(Protocol):
    capabilities: RuntimeCapabilities

    def interrupt(self, target: str) -> dict[str, Any]: ...
