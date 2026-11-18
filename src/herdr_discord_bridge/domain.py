from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

TARGET_KEY_VERSION = 1


class RuntimeKind(StrEnum):
    HERDR_TUI = "herdr-tui"
    HERDR_CORE = "herdr-core"


class LifecycleState(StrEnum):
    LIVE = "live"
    FINISHED = "finished"
    UNKNOWN = "unknown"


class AgentState(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    BLOCKED = "blocked"
    ERROR = "error"
    UNKNOWN = "unknown"


class RuntimeEventKind(StrEnum):
    SNAPSHOT = "snapshot"
    CREATED = "created"
    UPDATED = "updated"
    RESTARTED = "restarted"
    FINISHED = "finished"
    REMOVED = "removed"
    STATUS = "status"
    TURN = "turn"
    DISCONNECTED = "disconnected"
    RESYNCHRONIZED = "resynchronized"
    UNKNOWN = "unknown"


class OutputEventKind(StrEnum):
    RESPONSE = "response"
    QUESTION = "question"
    QUESTION_RESOLVED = "question_resolved"
    HUMAN_INPUT = "human_input"


class InboundActionKind(StrEnum):
    SEND_TEXT = "send_text"
    INTERRUPT = "interrupt"
    LIST_TARGETS = "list_targets"
    SELECT_TARGET = "select_target"
    BIND_TARGET = "bind_target"
    HELP = "help"
    STATUS = "status"


class MutationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class MutationOutcomeUnknownError(RuntimeError):
    """A mutation may have applied, so callers must not retry automatically."""


@dataclass(frozen=True, slots=True)
class TargetKey:
    runtime: RuntimeKind
    address: str
    controller_core_id: str | None = None
    origin_core_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.runtime, RuntimeKind, "target runtime")
        _require_nonempty(self.address, "target address")
        if self.runtime is RuntimeKind.HERDR_CORE:
            _require_nonempty(self.controller_core_id, "controller Core ID")
            if self.origin_core_id is not None:
                _require_nonempty(self.origin_core_id, "origin Core ID")
        elif self.controller_core_id is not None or self.origin_core_id is not None:
            raise ValueError("legacy Herdr targets cannot carry Core IDs")

    @classmethod
    def core(
        cls,
        *,
        controller_core_id: str,
        address: str,
        origin_core_id: str | None = None,
    ) -> TargetKey:
        return cls(
            runtime=RuntimeKind.HERDR_CORE,
            controller_core_id=controller_core_id,
            origin_core_id=origin_core_id,
            address=address,
        )

    @classmethod
    def legacy(cls, pane_id: str) -> TargetKey:
        return cls(runtime=RuntimeKind.HERDR_TUI, address=pane_id)

    def to_storage_key(self) -> str:
        return json.dumps(
            {
                "v": TARGET_KEY_VERSION,
                "runtime": self.runtime.value,
                "address": self.address,
                "controller_core_id": self.controller_core_id,
                "origin_core_id": self.origin_core_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_storage_key(cls, value: str) -> TargetKey:
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid target key JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError("target key must be a JSON object")
        if item.get("v") != TARGET_KEY_VERSION:
            raise ValueError(f"unsupported target key version: {item.get('v')!r}")
        try:
            runtime = RuntimeKind(item.get("runtime"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported runtime: {item.get('runtime')!r}") from exc
        return cls(
            runtime=runtime,
            address=_optional_field(item, "address", required=True),
            controller_core_id=_optional_field(item, "controller_core_id"),
            origin_core_id=_optional_field(item, "origin_core_id"),
        )


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    key: TargetKey
    lifecycle: LifecycleState
    agent_state: AgentState = AgentState.UNKNOWN
    agent_app: str | None = None
    status_message: str | None = None
    title: str | None = None
    turn: int = 0
    cwd: str | None = None
    foreground_name: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    created_ms: int | None = None
    status_changed_ms: int | None = None
    finished_ms: int | None = None
    reported: bool = False

    def __post_init__(self) -> None:
        _require_enum(self.lifecycle, LifecycleState, "target lifecycle")
        _require_enum(self.agent_state, AgentState, "agent state")
        if self.turn < 0:
            raise ValueError("target turn must not be negative")
        for label, value in self.labels.items():
            _require_nonempty(label, "target label name")
            if not isinstance(value, str):
                raise ValueError("target label values must be strings")


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: RuntimeEventKind
    sequence: int | None = None
    target: TargetKey | None = None
    snapshot: TargetSnapshot | None = None
    raw_kind: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, RuntimeEventKind, "runtime event kind")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("runtime event sequence must not be negative")
        if self.snapshot is not None and self.target not in (None, self.snapshot.key):
            raise ValueError("runtime event target and snapshot key disagree")


@dataclass(frozen=True, slots=True)
class RelayOutputEvent:
    event_id: str
    target: TargetKey
    agent: str
    kind: OutputEventKind
    text: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_enum(self.kind, OutputEventKind, "output event kind")
        _require_nonempty(self.event_id, "output event ID")
        _require_nonempty(self.agent, "output event agent")
        if self.kind is OutputEventKind.HUMAN_INPUT and self.text is not None:
            raise ValueError("human_input events must not store prompt text")


@dataclass(frozen=True, slots=True)
class Actor:
    transport: str
    actor_id: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.transport, "actor transport")
        _require_nonempty(self.actor_id, "actor ID")


@dataclass(frozen=True, slots=True)
class Location:
    transport: str
    container_id: str
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.transport, "location transport")
        _require_nonempty(self.container_id, "location container ID")
        if self.conversation_id is not None:
            _require_nonempty(self.conversation_id, "location conversation ID")


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    target: TargetKey
    transport: str
    account_id: str
    container_id: str
    conversation_id: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_nonempty(self.route_id, "route ID")
        _require_nonempty(self.transport, "route transport")
        _require_nonempty(self.account_id, "route account ID")
        _require_nonempty(self.container_id, "route container ID")
        if self.conversation_id is not None:
            _require_nonempty(self.conversation_id, "route conversation ID")


@dataclass(frozen=True, slots=True)
class InboundAction:
    kind: InboundActionKind
    actor: Actor
    location: Location
    target: TargetKey | None = None
    text: str | None = None
    source_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, InboundActionKind, "inbound action kind")
        if self.actor.transport != self.location.transport:
            raise ValueError("action actor and location transports disagree")
        if self.kind is InboundActionKind.SEND_TEXT:
            if self.target is None:
                raise ValueError("send_text requires a target")
            _require_nonempty(self.text, "send_text payload")


@dataclass(frozen=True, slots=True)
class MutationResult:
    outcome: MutationOutcome
    message: str | None = None
    observed_turn: int | None = None
    backend_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_enum(self.outcome, MutationOutcome, "mutation outcome")
        if self.observed_turn is not None and self.observed_turn < 0:
            raise ValueError("observed turn must not be negative")


def _require_nonempty(value: str | None, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _require_enum(value: Any, enum_type: type[StrEnum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{label} must be a {enum_type.__name__}")


def _optional_field(
    item: Mapping[str, Any], key: str, *, required: bool = False
) -> str | None:
    value = item.get(key)
    if value is None:
        if required:
            raise ValueError(f"target key {key} must be a string")
        return None
    if not isinstance(value, str):
        raise ValueError(f"target key {key} must be a string or null")
    return value
