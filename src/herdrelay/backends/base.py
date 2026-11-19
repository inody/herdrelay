from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain import (
    BackendKind,
    MutationResult,
    RuntimeEvent,
    TargetKey,
    TargetSnapshot,
)
from ..runtime import RuntimeCapabilities, RuntimeClient


@runtime_checkable
class RuntimeBackend(Protocol):
    """Transport-neutral asynchronous runtime operations."""

    kind: BackendKind
    capabilities: RuntimeCapabilities

    async def list_targets(self) -> list[TargetSnapshot]: ...

    async def get_target(self, key: TargetKey) -> TargetSnapshot: ...

    async def send_text(
        self,
        key: TargetKey,
        text: str,
        *,
        expected_generation: str | None = None,
    ) -> MutationResult: ...

    async def interrupt(
        self,
        key: TargetKey,
        *,
        expected_generation: str | None = None,
    ) -> MutationResult: ...


@runtime_checkable
class BackendEventSource(Protocol):
    async def events(self) -> AsyncIterator[RuntimeEvent]: ...


@runtime_checkable
class BackendService(Protocol):
    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendProvider:
    """Selected backend plus the compatibility client used during migration."""

    kind: BackendKind
    backend: RuntimeBackend
    client: RuntimeClient
    events: BackendEventSource | None = None
    services: tuple[BackendService, ...] = ()
