from __future__ import annotations

import asyncio
from typing import Any

from ..domain import (
    AgentState,
    BackendKind,
    LifecycleState,
    MutationOutcome,
    MutationOutcomeUnknownError,
    MutationRejectedError,
    MutationResult,
    TargetKey,
    TargetSnapshot,
)
from ..models import HerdrTarget
from ..runtime import InterruptRuntime, RuntimeCapabilities, RuntimeClient


class CompatibilityBackend:
    """Expose existing synchronous Herdr clients through the backend contract."""

    def __init__(
        self,
        *,
        kind: BackendKind,
        client: RuntimeClient,
    ):
        self.kind = kind
        self.client = client
        self.capabilities: RuntimeCapabilities = client.capabilities

    async def list_targets(self) -> list[TargetSnapshot]:
        targets = await asyncio.to_thread(self.client.list_targets)
        return [self._snapshot(target) for target in targets]

    async def get_target(self, key: TargetKey) -> TargetSnapshot:
        if key.backend is not self.kind:
            raise ValueError(
                f"target belongs to {key.backend.value}, not {self.kind.value}"
            )
        target = await asyncio.to_thread(self.client.resolve_target_key, key)
        return self._snapshot(target)

    async def send_text(
        self,
        key: TargetKey,
        text: str,
        *,
        expected_generation: str | None = None,
    ) -> MutationResult:
        if not self.capabilities.normal_send:
            return MutationResult(
                outcome=MutationOutcome.REJECTED,
                message=f"{self.kind.value} does not support prompt submission",
            )
        snapshot = await self.get_target(key)
        mismatch = _generation_mismatch(snapshot, expected_generation)
        if mismatch is not None:
            return mismatch

        try:
            receipt = await self._send(snapshot.key.address, text)
        except MutationOutcomeUnknownError as exc:
            return MutationResult(
                outcome=MutationOutcome.OUTCOME_UNKNOWN,
                message=str(exc),
            )
        except Exception as exc:
            return _failed_mutation(exc)
        return MutationResult(
            outcome=MutationOutcome.SUCCEEDED,
            observed_turn=getattr(receipt, "observed_turn", None),
            backend_data=_result_mapping(getattr(receipt, "result", None)),
        )

    async def interrupt(
        self,
        key: TargetKey,
        *,
        expected_generation: str | None = None,
    ) -> MutationResult:
        if not self.capabilities.native_interrupt or not isinstance(
            self.client, InterruptRuntime
        ):
            return MutationResult(
                outcome=MutationOutcome.REJECTED,
                message=f"{self.kind.value} does not support guarded interrupt",
            )

        snapshot = await self.get_target(key)
        mismatch = _generation_mismatch(snapshot, expected_generation)
        if mismatch is not None:
            return mismatch

        try:
            receipt = await self._interrupt(snapshot.key.address)
        except MutationOutcomeUnknownError as exc:
            return MutationResult(
                outcome=MutationOutcome.OUTCOME_UNKNOWN,
                message=str(exc),
            )
        except Exception as exc:
            return _failed_mutation(exc)
        return MutationResult(
            outcome=MutationOutcome.SUCCEEDED,
            observed_turn=getattr(receipt, "observed_turn", None),
            backend_data=_result_mapping(getattr(receipt, "result", receipt)),
        )

    async def _send(self, target: str, text: str) -> Any:
        send_with_receipt = getattr(self.client, "send_with_receipt", None)
        if callable(send_with_receipt):
            return await asyncio.to_thread(send_with_receipt, target, text)
        await asyncio.to_thread(self.client.send, target, text)
        return None

    async def _interrupt(self, target: str) -> Any:
        interrupt_with_receipt = getattr(self.client, "interrupt_with_receipt", None)
        if callable(interrupt_with_receipt):
            return await asyncio.to_thread(interrupt_with_receipt, target)
        assert isinstance(self.client, InterruptRuntime)
        return await asyncio.to_thread(self.client.interrupt, target)

    def _snapshot(self, target: HerdrTarget) -> TargetSnapshot:
        labels = {
            label: value
            for label, value in {
                "workspace": target.workspace_label,
                "tab": target.tab_label,
            }.items()
            if value
        }
        return TargetSnapshot(
            key=self.client.target_key(target.target),
            lifecycle=LifecycleState.LIVE,
            agent_state=_agent_state(target.status),
            agent_app=target.agent_name,
            title=target.label,
            cwd=target.cwd,
            labels=labels,
            reported=bool(target.agent_name),
        )


def _agent_state(value: str | None) -> AgentState:
    if not value:
        return AgentState.UNKNOWN
    normalized = value.casefold()
    try:
        return AgentState(normalized)
    except ValueError:
        return AgentState.UNKNOWN


def _generation_mismatch(
    snapshot: TargetSnapshot, expected: str | None
) -> MutationResult | None:
    if expected is None or expected == snapshot.generation:
        return None
    return MutationResult(
        outcome=MutationOutcome.REJECTED,
        message="target generation changed; no mutation was attempted",
    )


def _failed_mutation(exc: Exception) -> MutationResult:
    outcome = (
        MutationOutcome.REJECTED
        if isinstance(exc, MutationRejectedError)
        or getattr(exc, "kind", None)
        else MutationOutcome.FAILED
    )
    return MutationResult(outcome=outcome, message=str(exc))


def _result_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
