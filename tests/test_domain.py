from __future__ import annotations

import ast
from pathlib import Path

import pytest

from herdr_discord_bridge.domain import (
    Actor,
    AgentState,
    InboundAction,
    InboundActionKind,
    LifecycleState,
    Location,
    MutationOutcome,
    MutationResult,
    OutputEventKind,
    RelayOutputEvent,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeKind,
    TargetKey,
    TargetSnapshot,
)

ROOT = Path(__file__).parents[1]
DOMAIN_SOURCE = ROOT / "src" / "herdr_discord_bridge" / "domain.py"


def test_core_target_key_roundtrips_with_unicode_and_qualified_address():
    key = TargetKey.core(
        controller_core_id="controller-α",
        origin_core_id="origin-β",
        address="remote/t12",
    )

    encoded = key.to_storage_key()

    assert TargetKey.from_storage_key(encoded) == key
    assert '"v":1' in encoded
    assert key.runtime is RuntimeKind.HERDR_CORE


def test_legacy_target_key_roundtrips_without_core_identity():
    key = TargetKey.legacy("w0:p6")

    assert TargetKey.from_storage_key(key.to_storage_key()) == key
    assert key.controller_core_id is None
    assert key.origin_core_id is None


def test_route_values_keep_legacy_compatibility_and_pin_core_identity():
    legacy = TargetKey.legacy("w0:p6")
    core = TargetKey.core(controller_core_id="core-1", address="t1")

    assert legacy.to_route_value() == "w0:p6"
    assert TargetKey.from_route_value("w0:p6") == legacy
    assert TargetKey.from_route_value(core.to_route_value()) == core


def test_target_key_rejects_cross_runtime_identity_and_unknown_versions():
    with pytest.raises(ValueError, match="controller Core ID"):
        TargetKey(runtime=RuntimeKind.HERDR_CORE, address="t1")
    with pytest.raises(ValueError, match="cannot carry Core IDs"):
        TargetKey(
            runtime=RuntimeKind.HERDR_TUI,
            address="w0:p1",
            controller_core_id="core",
        )
    with pytest.raises(ValueError, match="unsupported target key version"):
        TargetKey.from_storage_key(
            '{"v":2,"runtime":"herdr-core","address":"t1",'
            '"controller_core_id":"core"}'
        )


def test_target_snapshot_keeps_mutable_display_metadata_outside_identity():
    key = TargetKey.core(controller_core_id="core", address="t12")
    before = TargetSnapshot(
        key=key,
        lifecycle=LifecycleState.LIVE,
        agent_state=AgentState.WORKING,
        title="First title",
        cwd="/tmp/one",
        labels={"where": "one / 1"},
        turn=3,
        reported=True,
    )
    after = TargetSnapshot(
        key=key,
        lifecycle=LifecycleState.LIVE,
        agent_state=AgentState.DONE,
        title="Second title",
        cwd="/tmp/two",
        labels={"where": "two / 2"},
        turn=4,
        reported=True,
    )

    assert before.key == after.key
    assert before != after


def test_runtime_event_rejects_snapshot_for_another_target():
    first = TargetKey.core(controller_core_id="core", address="t1")
    second = TargetKey.core(controller_core_id="core", address="t2")
    snapshot = TargetSnapshot(key=second, lifecycle=LifecycleState.LIVE)

    with pytest.raises(ValueError, match="disagree"):
        RuntimeEvent(
            kind=RuntimeEventKind.UPDATED,
            target=first,
            snapshot=snapshot,
        )


def test_human_input_output_event_cannot_store_prompt_text():
    target = TargetKey.core(controller_core_id="core", address="t1")

    event = RelayOutputEvent(
        event_id="event-1",
        target=target,
        agent="pi",
        kind=OutputEventKind.HUMAN_INPUT,
    )
    assert event.text is None

    with pytest.raises(ValueError, match="must not store prompt text"):
        RelayOutputEvent(
            event_id="event-2",
            target=target,
            agent="pi",
            kind=OutputEventKind.HUMAN_INPUT,
            text="private prompt",
        )


def test_send_action_requires_matching_transport_target_and_text():
    target = TargetKey.core(controller_core_id="core", address="t1")
    actor = Actor(transport="telegram", actor_id="42")
    location = Location(
        transport="telegram",
        container_id="-100123",
        conversation_id="7",
    )

    action = InboundAction(
        kind=InboundActionKind.SEND_TEXT,
        actor=actor,
        location=location,
        target=target,
        text="continue",
        source_message_id="99",
    )
    assert action.target == target

    with pytest.raises(ValueError, match="requires a target"):
        InboundAction(
            kind=InboundActionKind.SEND_TEXT,
            actor=actor,
            location=location,
            text="continue",
        )
    with pytest.raises(ValueError, match="transports disagree"):
        InboundAction(
            kind=InboundActionKind.STATUS,
            actor=Actor(transport="discord", actor_id="42"),
            location=location,
        )


def test_mutation_result_represents_unknown_outcome_without_implying_failure():
    result = MutationResult(
        outcome=MutationOutcome.OUTCOME_UNKNOWN,
        message="connection closed after submission",
        observed_turn=12,
    )

    assert result.outcome is MutationOutcome.OUTCOME_UNKNOWN
    assert result.outcome is not MutationOutcome.FAILED


def test_domain_module_has_no_chat_platform_imports():
    tree = ast.parse(DOMAIN_SOURCE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "discord" not in imported
    assert "telegram" not in imported
