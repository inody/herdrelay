from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .models import HerdrTarget, StartedAgent


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    normal_send: bool = True
    bounded_read: bool = True
    native_events: bool = False
    native_interrupt: bool = False
    raw_input: bool = False
    guarded_approval: bool = False
    agent_start: bool = False
    target_close: bool = False


@runtime_checkable
class RuntimeClient(Protocol):
    """Compatibility boundary used while orchestration is made transport-neutral.

    The safe common subset is list/read/send. Legacy-only methods remain here
    temporarily so the existing Discord adapter can preserve behavior while
    checking backend capabilities and configuration before invoking them.
    """

    capabilities: RuntimeCapabilities

    def list_targets(self) -> list[HerdrTarget]: ...

    def list_panes(self) -> list[HerdrTarget]: ...

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

    def send_keys(
        self, target: str, keys: Iterable[str], *, interval_seconds: float = 0
    ) -> None: ...

    def send_raw_text(self, target: str, text: str) -> None: ...

    def send_text_enter(self, target: str, text: str) -> None: ...

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

    def pane_close(self, pane_id: str) -> None: ...


@runtime_checkable
class InterruptRuntime(Protocol):
    capabilities: RuntimeCapabilities

    def interrupt(self, target: str) -> dict[str, Any]: ...
