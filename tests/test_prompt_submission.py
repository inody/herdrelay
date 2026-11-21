from __future__ import annotations

from dataclasses import dataclass

import pytest

from herdrelay.domain import (
    Actor,
    BackendUnavailableError,
    Location,
    MutationOutcomeUnknownError,
    MutationReceipt,
    MutationRejectedError,
    TargetKey,
)
from herdrelay.models import HerdrTarget
from herdrelay.prompt_submission import (
    PromptOutcome,
    PromptSubmission,
    PromptSubmissionService,
)
from herdrelay.runtime import RuntimeCapabilities
from herdrelay.store import Store


@dataclass
class FakeClient:
    key: TargetKey
    resolved_key: TargetKey | None = None
    agent_name: str | None = "pi"
    resolve_error: Exception | None = None
    send_error: Exception | None = None

    capabilities = RuntimeCapabilities()

    def __post_init__(self):
        self.send_calls: list[tuple[str, str]] = []
        self.resolve_calls: list[TargetKey] = []

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget:
        self.resolve_calls.append(key)
        if self.resolve_error is not None:
            raise self.resolve_error
        return HerdrTarget(target=key.address, agent_name=self.agent_name)

    def target_key(self, target: str) -> TargetKey:
        return self.resolved_key or self.key

    def send(self, target: str, message: str) -> MutationReceipt:
        self.send_calls.append((target, message))
        if self.send_error is not None:
            raise self.send_error
        return MutationReceipt(
            observed_turn=4,
            result={"accepted": True, "request_id": "request-1"},
        )


def submission(key: TargetKey, *, event_key: str = "telegram:message:1"):
    return PromptSubmission(
        event_key=event_key,
        actor=Actor(transport="telegram", actor_id="42"),
        location=Location(
            transport="telegram",
            container_id="-1001",
            conversation_id="7",
        ),
        target=key,
        text="  preserve this prompt exactly  ",
    )


def make_service(tmp_path, client, *, rate_limit=None):
    store = Store(tmp_path / "relay.sqlite3")
    service = PromptSubmissionService(
        store=store,
        client=client,
        rate_limit=rate_limit,
    )
    return service, store


def test_prompt_submission_claims_revalidates_sends_and_finishes_audit(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    client = FakeClient(key)
    service, store = make_service(tmp_path, client)
    request = submission(key)

    result = service.submit(request)

    assert result.outcome is PromptOutcome.SUCCEEDED
    assert result.claimed is True
    assert result.observed_turn == 4
    assert result.backend_data == {"accepted": True, "request_id": "request-1"}
    assert client.resolve_calls == [key]
    assert client.send_calls == [("t1", request.text)]
    [audit] = store.list_transport_audits()
    assert audit["id"] == result.audit_id
    assert audit["transport"] == "telegram"
    assert audit["actor_id"] == "42"
    assert audit["container_id"] == "-1001"
    assert audit["conversation_id"] == "7"
    assert audit["action"] == "send_text"
    assert audit["target_key"] == key.to_storage_key()
    assert audit["payload_preview"] == request.text
    assert audit["observed_turn"] == 4
    assert audit["outcome"] == "succeeded"
    assert audit["detail"] == "{'accepted': True, 'request_id': 'request-1'}"


def test_prompt_submission_deduplicates_before_second_backend_call(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    client = FakeClient(key)
    service, store = make_service(tmp_path, client)
    request = submission(key)

    first = service.submit(request)
    second = service.submit(request)

    assert first.outcome is PromptOutcome.SUCCEEDED
    assert second.outcome is PromptOutcome.DEDUPLICATED
    assert second.claimed is False
    assert client.send_calls == [("t1", request.text)]
    assert len(store.list_transport_audits()) == 1


def test_prompt_submission_rejects_changed_exact_identity_without_sending(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    changed = TargetKey.core(controller_core_id="core-2", address="t1")
    client = FakeClient(key, resolved_key=changed)
    service, store = make_service(tmp_path, client)

    result = service.submit(submission(key))

    assert result.outcome is PromptOutcome.REJECTED
    assert "identity changed" in (result.detail or "")
    assert client.send_calls == []
    assert store.list_transport_audits()[0]["outcome"] == "rejected"


def test_prompt_submission_preserves_unknown_outcome_and_never_retries(tmp_path):
    key = TargetKey.orca(instance_id="runtime-1", address="host/tab:pane")
    client = FakeClient(
        key,
        send_error=MutationOutcomeUnknownError("response lost after submission"),
    )
    service, store = make_service(tmp_path, client)

    result = service.submit(submission(key))

    assert result.outcome is PromptOutcome.OUTCOME_UNKNOWN
    assert client.send_calls == [("host/tab:pane", submission(key).text)]
    audit = store.list_transport_audits()[0]
    assert audit["outcome"] == "outcome_unknown"
    assert audit["detail"] == "response lost after submission"


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (MutationRejectedError("blocked"), PromptOutcome.REJECTED),
        (BackendUnavailableError("backend unavailable"), PromptOutcome.FAILED),
    ],
)
def test_prompt_submission_normalizes_backend_contract_errors(
    tmp_path, error, outcome
):
    key = TargetKey.legacy("w1:p1")
    client = FakeClient(key, send_error=error)
    service, store = make_service(tmp_path, client)

    result = service.submit(submission(key))

    assert result.outcome is outcome
    assert result.detail == str(error)
    assert store.list_transport_audits()[0]["outcome"] == outcome.value


def test_prompt_submission_fails_closed_on_unexpected_resolution_error(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    client = FakeClient(key, resolve_error=RuntimeError("invalid backend snapshot"))
    service, store = make_service(tmp_path, client)

    result = service.submit(submission(key))

    assert result.outcome is PromptOutcome.FAILED
    assert client.send_calls == []
    assert store.list_transport_audits()[0]["detail"] == "invalid backend snapshot"


def test_prompt_submission_claims_preflight_policy_rejection(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    client = FakeClient(key)
    service, store = make_service(tmp_path, client)
    request = PromptSubmission(
        event_key="discord:message:1",
        actor=Actor(transport="discord", actor_id="42"),
        location=Location(
            transport="discord", container_id="1", conversation_id="100"
        ),
        target=key,
        text="blocked prompt",
        preflight_rejection="text blocklist",
    )

    result = service.submit(request)

    assert result.outcome is PromptOutcome.REJECTED
    assert result.detail == "text blocklist"
    assert client.resolve_calls == []
    assert client.send_calls == []
    assert store.has_event_key(request.event_key) is True
    assert store.list_transport_audits()[0]["outcome"] == "rejected"


def test_prompt_submission_rate_limit_rejection_is_audited_without_claim(tmp_path):
    key = TargetKey.core(controller_core_id="core-1", address="t1")
    client = FakeClient(key)
    service, store = make_service(
        tmp_path,
        client,
        rate_limit=lambda request: "actor rate limit exceeded",
    )
    request = submission(key)

    result = service.submit(request)

    assert result.outcome is PromptOutcome.REJECTED
    assert result.detail == "actor rate limit exceeded"
    assert client.resolve_calls == []
    assert client.send_calls == []
    assert store.has_event_key(request.event_key) is False
    assert store.list_transport_audits()[0]["outcome"] == "rejected"
