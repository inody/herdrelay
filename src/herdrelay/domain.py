from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

LEGACY_TARGET_KEY_VERSION = 1
TARGET_KEY_VERSION = 2


class BackendKind(StrEnum):
    HERDR_TUI = "herdr-tui"
    HERDR_CORE = "herdr-core"
    ORCA = "orca"


# Compatibility name for existing callers while the runtime/backend vocabulary
# is migrated across the package.
RuntimeKind = BackendKind


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


class MutationError(RuntimeError):
    """Base class for normalized backend mutation failures."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class MutationRejectedError(MutationError):
    """The backend definitively rejected the mutation before applying it."""


class MutationFailedError(MutationError):
    """The backend definitively failed without applying the mutation."""


class BackendUnavailableError(MutationFailedError):
    """The selected backend or exact target is currently unavailable."""


class MutationOutcomeUnknownError(MutationError):
    """A mutation may have applied, so callers must not retry automatically."""


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    observed_turn: int | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TargetKey:
    backend: BackendKind
    address: str
    instance_id: str | None = None
    origin_instance_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.backend, BackendKind, "target backend")
        _require_nonempty(self.address, "target address")
        if self.backend in {BackendKind.HERDR_CORE, BackendKind.ORCA}:
            _require_nonempty(self.instance_id, "target instance ID")
        elif self.instance_id is not None or self.origin_instance_id is not None:
            raise ValueError("legacy Herdr targets cannot carry instance IDs")
        if self.origin_instance_id is not None:
            _require_nonempty(self.origin_instance_id, "origin instance ID")
            if self.backend is not BackendKind.HERDR_CORE:
                raise ValueError("only Herdr Core targets can carry an origin instance ID")

    @property
    def runtime(self) -> BackendKind:
        """Compatibility alias for callers using the former runtime name."""
        return self.backend

    @property
    def controller_core_id(self) -> str | None:
        return self.instance_id if self.backend is BackendKind.HERDR_CORE else None

    @property
    def origin_core_id(self) -> str | None:
        return (
            self.origin_instance_id
            if self.backend is BackendKind.HERDR_CORE
            else None
        )

    @classmethod
    def core(
        cls,
        *,
        controller_core_id: str,
        address: str,
        origin_core_id: str | None = None,
    ) -> TargetKey:
        return cls(
            backend=BackendKind.HERDR_CORE,
            instance_id=controller_core_id,
            origin_instance_id=origin_core_id,
            address=address,
        )

    @classmethod
    def legacy(cls, pane_id: str) -> TargetKey:
        return cls(backend=BackendKind.HERDR_TUI, address=pane_id)

    @classmethod
    def orca(cls, *, instance_id: str, address: str) -> TargetKey:
        return cls(
            backend=BackendKind.ORCA,
            instance_id=instance_id,
            address=address,
        )

    def to_route_value(self) -> str:
        """Persist legacy pane IDs compatibly and other identities exactly."""
        if self.backend is BackendKind.HERDR_TUI:
            return self.address
        return self.to_storage_key()

    def to_storage_key(self) -> str:
        if self.backend is not BackendKind.ORCA:
            return json.dumps(
                {
                    "v": LEGACY_TARGET_KEY_VERSION,
                    "runtime": self.backend.value,
                    "address": self.address,
                    "controller_core_id": self.controller_core_id,
                    "origin_core_id": self.origin_core_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        return json.dumps(
            {
                "v": TARGET_KEY_VERSION,
                "backend": self.backend.value,
                "instance_id": self.instance_id,
                "origin_instance_id": self.origin_instance_id,
                "address": self.address,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_route_value(cls, value: str) -> TargetKey:
        """Read a normalized target key or an existing raw legacy pane ID."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("target route value must be a non-empty string")
        if not value.lstrip().startswith("{"):
            return cls.legacy(value)
        return cls.from_storage_key(value)

    @classmethod
    def from_storage_key(cls, value: str) -> TargetKey:
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid target key JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ValueError("target key must be a JSON object")
        version = item.get("v")
        if version == LEGACY_TARGET_KEY_VERSION:
            return cls._from_legacy_storage_item(item)
        if version != TARGET_KEY_VERSION:
            raise ValueError(f"unsupported target key version: {version!r}")
        try:
            backend = BackendKind(item.get("backend"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported backend: {item.get('backend')!r}") from exc
        return cls(
            backend=backend,
            address=_optional_field(item, "address", required=True),
            instance_id=_optional_field(item, "instance_id"),
            origin_instance_id=_optional_field(item, "origin_instance_id"),
        )

    @classmethod
    def _from_legacy_storage_item(cls, item: Mapping[str, Any]) -> TargetKey:
        try:
            backend = BackendKind(item.get("runtime"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported runtime: {item.get('runtime')!r}") from exc
        if backend is BackendKind.ORCA:
            raise ValueError("Orca target keys require version 2")
        return cls(
            backend=backend,
            address=_optional_field(item, "address", required=True),
            instance_id=_optional_field(item, "controller_core_id"),
            origin_instance_id=_optional_field(item, "origin_core_id"),
        )


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    key: TargetKey
    lifecycle: LifecycleState
    generation: str | None = None
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
        if self.generation is not None:
            _require_nonempty(self.generation, "target generation")
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
