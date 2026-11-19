from __future__ import annotations

from dataclasses import dataclass

import pytest

from herdrelay.backends import CompatibilityBackend, build_backend_provider
from herdrelay.config import AppConfig, OrcaRuntimeConfig, RuntimeConfig
from herdrelay.domain import (
    AgentState,
    BackendKind,
    MutationOutcome,
    MutationOutcomeUnknownError,
    TargetKey,
)
from herdrelay.models import HerdrTarget
from herdrelay.runtime import RuntimeCapabilities
from herdrelay.store import Store


@dataclass
class Receipt:
    observed_turn: int
    result: dict


class FakeClient:
    capabilities = RuntimeCapabilities(native_interrupt=True)

    def __init__(self):
        self.sent = []
        self.interrupted = []

    def list_targets(self):
        return [
            HerdrTarget(
                target="t1",
                label="project / agent",
                agent_name="pi",
                status="working",
                workspace_label="project",
                tab_label="agent",
                cwd="/tmp/project",
            )
        ]

    def target_key(self, target):
        return TargetKey.core(controller_core_id="core-1", address=target)

    def resolve_target_key(self, key):
        if key != self.target_key("t1"):
            raise ValueError("target missing")
        return self.list_targets()[0]

    def send_with_receipt(self, target, text):
        self.sent.append((target, text))
        return Receipt(observed_turn=4, result={"accepted": True})

    def interrupt_with_receipt(self, target):
        self.interrupted.append(target)
        return Receipt(observed_turn=5, result={"interrupted": True})

    def interrupt(self, target):
        return self.interrupt_with_receipt(target).result


@pytest.mark.asyncio
async def test_compatibility_backend_normalizes_targets_and_mutations():
    client = FakeClient()
    backend = CompatibilityBackend(kind=BackendKind.HERDR_CORE, client=client)

    [snapshot] = await backend.list_targets()
    sent = await backend.send_text(snapshot.key, "continue")
    interrupted = await backend.interrupt(snapshot.key)

    assert snapshot.agent_state is AgentState.WORKING
    assert snapshot.agent_app == "pi"
    assert snapshot.labels == {"workspace": "project", "tab": "agent"}
    assert snapshot.cwd == "/tmp/project"
    assert sent.outcome is MutationOutcome.SUCCEEDED
    assert sent.observed_turn == 4
    assert sent.backend_data == {"accepted": True}
    assert interrupted.outcome is MutationOutcome.SUCCEEDED
    assert interrupted.observed_turn == 5
    assert client.sent == [("t1", "continue")]
    assert client.interrupted == ["t1"]


@pytest.mark.asyncio
async def test_compatibility_backend_rejects_wrong_backend_and_stale_generation():
    client = FakeClient()
    backend = CompatibilityBackend(kind=BackendKind.HERDR_CORE, client=client)

    with pytest.raises(ValueError, match="belongs to herdr-tui"):
        await backend.get_target(TargetKey.legacy("w1:p1"))

    result = await backend.send_text(
        TargetKey.core(controller_core_id="core-1", address="t1"),
        "continue",
        expected_generation="old-session",
    )

    assert result.outcome is MutationOutcome.REJECTED
    assert client.sent == []


@pytest.mark.asyncio
async def test_compatibility_backend_preserves_unknown_mutation_outcome():
    class UnknownClient(FakeClient):
        def send_with_receipt(self, target, text):
            raise MutationOutcomeUnknownError("connection closed")

    backend = CompatibilityBackend(
        kind=BackendKind.HERDR_CORE,
        client=UnknownClient(),
    )

    result = await backend.send_text(
        TargetKey.core(controller_core_id="core-1", address="t1"),
        "continue",
    )

    assert result.outcome is MutationOutcome.OUTCOME_UNKNOWN
    assert result.message == "connection closed"


def test_build_backend_provider_wraps_selected_runtime(monkeypatch):
    client = FakeClient()
    config = AppConfig(
        discord_token="token",
        runtime=RuntimeConfig(provider="herdr-core"),
    )
    monkeypatch.setattr(
        "herdrelay.backends.provider.build_runtime_client",
        lambda supplied: client,
    )

    provider = build_backend_provider(config)

    assert provider.kind is BackendKind.HERDR_CORE
    assert provider.client is client
    assert provider.backend.client is client


def test_build_backend_provider_adds_orca_output_source_for_hook_streaming(
    tmp_path,
):
    config = AppConfig(
        discord_token="token",
        database_path=str(tmp_path / "store.sqlite3"),
        enable_streaming=True,
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(allowed_pane_keys=frozenset({"tab:pane"})),
        ),
    )

    provider = build_backend_provider(
        config, store=Store(config.database_path)
    )

    assert len(provider.services) == 1


def test_build_backend_provider_wraps_orca_runtime():
    config = AppConfig(
        discord_token="token",
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(allowed_pane_keys=frozenset({"tab:pane"})),
        ),
    )

    provider = build_backend_provider(config)

    assert provider.kind is BackendKind.ORCA
    assert provider.client.capabilities.normal_send is True
