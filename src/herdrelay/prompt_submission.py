from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .domain import (
    Actor,
    Location,
    MutationFailedError,
    MutationOutcomeUnknownError,
    MutationReceipt,
    MutationRejectedError,
    TargetKey,
)
from .formatter import truncate
from .models import TransportAuditEntry
from .runtime import RuntimeClient
from .store import Store


class PromptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    DEDUPLICATED = "deduplicated"


@dataclass(frozen=True, slots=True)
class PromptSubmission:
    event_key: str
    actor: Actor
    location: Location
    target: TargetKey
    text: str
    audit_action: str = "send_text"
    preflight_rejection: str | None = None

    def __post_init__(self) -> None:
        if not self.event_key.strip():
            raise ValueError("prompt event key must be non-empty")
        if self.actor.transport != self.location.transport:
            raise ValueError("prompt actor and location transports disagree")
        if not self.audit_action.strip():
            raise ValueError("prompt audit action must be non-empty")
        if (
            self.preflight_rejection is not None
            and not self.preflight_rejection.strip()
        ):
            raise ValueError("prompt preflight rejection must be non-empty")


@dataclass(frozen=True, slots=True)
class PromptResult:
    outcome: PromptOutcome
    audit_id: int | None = None
    detail: str | None = None
    observed_turn: int | None = None
    backend_data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def claimed(self) -> bool:
        return self.outcome is not PromptOutcome.DEDUPLICATED


RateLimitCheck = Callable[[PromptSubmission], str | None]


class PromptSubmissionService:
    """Execute one claimed, audited direct-Agent prompt mutation."""

    def __init__(
        self,
        *,
        store: Store,
        client: RuntimeClient,
        rate_limit: RateLimitCheck | None = None,
    ):
        self.store = store
        self.client = client
        self.rate_limit = rate_limit

    def submit(self, submission: PromptSubmission) -> PromptResult:
        rate_limit_detail = (
            self.rate_limit(submission) if self.rate_limit is not None else None
        )
        if rate_limit_detail is not None:
            audit_id = self.store.add_transport_audit(
                self._audit_entry(
                    submission,
                    outcome=PromptOutcome.REJECTED.value,
                    detail=rate_limit_detail,
                )
            )
            return PromptResult(
                outcome=PromptOutcome.REJECTED,
                audit_id=audit_id,
                detail=rate_limit_detail,
            )

        audit_id = self.store.claim_transport_mutation(
            event_key=submission.event_key,
            entry=self._audit_entry(submission, outcome="attempt"),
        )
        if audit_id is None:
            return PromptResult(outcome=PromptOutcome.DEDUPLICATED)

        if submission.preflight_rejection is not None:
            return self._finish(
                audit_id,
                outcome=PromptOutcome.REJECTED,
                detail=submission.preflight_rejection,
            )
        if not submission.text.strip():
            return self._finish(
                audit_id,
                outcome=PromptOutcome.REJECTED,
                detail="prompt text is empty",
            )

        try:
            target = self.client.resolve_target_key(submission.target)
            live_key = self.client.target_key(target.target)
            if live_key != submission.target:
                raise MutationRejectedError(
                    "resolved target identity changed; no mutation was attempted"
                )
            if not target.agent_name:
                raise MutationRejectedError(
                    "resolved target has no detected agent; no mutation was attempted"
                )
            receipt = self.client.send(target.target, submission.text)
        except MutationOutcomeUnknownError as exc:
            return self._finish(
                audit_id,
                outcome=PromptOutcome.OUTCOME_UNKNOWN,
                detail=str(exc),
            )
        except MutationRejectedError as exc:
            return self._finish(
                audit_id,
                outcome=PromptOutcome.REJECTED,
                detail=str(exc),
            )
        except MutationFailedError as exc:
            return self._finish(
                audit_id,
                outcome=PromptOutcome.FAILED,
                detail=str(exc),
            )
        except Exception as exc:
            return self._finish(
                audit_id,
                outcome=PromptOutcome.FAILED,
                detail=str(exc),
            )

        return self._finish_success(audit_id, receipt)

    def _finish_success(
        self, audit_id: int, receipt: MutationReceipt
    ) -> PromptResult:
        backend_data = dict(receipt.result)
        detail = truncate(str(backend_data), max_chars=500) if backend_data else None
        self.store.finish_transport_audit(
            audit_id,
            outcome=PromptOutcome.SUCCEEDED.value,
            detail=detail,
            observed_turn=receipt.observed_turn,
        )
        return PromptResult(
            outcome=PromptOutcome.SUCCEEDED,
            audit_id=audit_id,
            detail=detail,
            observed_turn=receipt.observed_turn,
            backend_data=backend_data,
        )

    def _finish(
        self,
        audit_id: int,
        *,
        outcome: PromptOutcome,
        detail: str,
    ) -> PromptResult:
        bounded_detail = truncate(detail, max_chars=500)
        self.store.finish_transport_audit(
            audit_id,
            outcome=outcome.value,
            detail=bounded_detail,
        )
        return PromptResult(
            outcome=outcome,
            audit_id=audit_id,
            detail=bounded_detail,
        )

    @staticmethod
    def _audit_entry(
        submission: PromptSubmission,
        *,
        outcome: str,
        detail: str | None = None,
    ) -> TransportAuditEntry:
        return TransportAuditEntry(
            transport=submission.actor.transport,
            actor_id=submission.actor.actor_id,
            container_id=submission.location.container_id,
            conversation_id=submission.location.conversation_id,
            action=submission.audit_action,
            target_key=submission.target.to_storage_key(),
            payload_preview=truncate(submission.text, max_chars=200),
            outcome=outcome,
            detail=detail,
        )
