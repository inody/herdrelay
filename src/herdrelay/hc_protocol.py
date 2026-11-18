from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

PROTOCOL_GENERATION = 1
MAX_JSON_LINE_BYTES = 16 * 1024 * 1024

# Methods required by the planned HerdRelay Core MVP. Keeping this list in the
# protocol layer lets the static schema probe and a future live hello handshake
# enforce the same contract.
REQUIRED_METHODS = frozenset(
    {
        "hello",
        "server.status",
        "terminal.list",
        "terminal.get",
        "terminal.read",
        "terminal.send",
        "terminal.interrupt",
        "events.subscribe",
    }
)

# Parameters that HerdRelay plans to send. Optional parameters still need to be
# advertised by the schema when the implementation relies on them.
REQUIRED_METHOD_PARAMS: Mapping[str, frozenset[str]] = {
    "hello": frozenset({"generation"}),
    "terminal.get": frozenset({"terminal"}),
    "terminal.read": frozenset({"terminal", "lines"}),
    "terminal.send": frozenset({"terminal", "text"}),
    "terminal.interrupt": frozenset({"terminal"}),
}


class HcProtocolError(ValueError):
    """Raised when Herdr Core data does not satisfy the supported contract."""


@dataclass(frozen=True)
class HcCoreInfo:
    id: str
    name: str


@dataclass(frozen=True)
class HcHello:
    generation: int
    version: str
    core: HcCoreInfo
    framed: bool
    methods: frozenset[str]
    features: frozenset[str]


@dataclass(frozen=True)
class HcStatus:
    state: str
    app: str = ""
    title: str = ""
    message: str = ""
    kind: str | None = None
    progress: int | None = None


@dataclass(frozen=True)
class HcForeground:
    pid: int
    name: str
    argv: tuple[str, ...]
    cwd: str


@dataclass(frozen=True)
class HcTerminal:
    address: str
    labels: Mapping[str, str]
    argv: tuple[str, ...]
    cwd: str
    state: str
    created_ms: int
    screen: str
    cols: int
    rows: int
    name: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    reason: str | None = None
    finished_ms: int | None = None
    status: HcStatus | None = None
    status_ms: int | None = None
    records: Mapping[str, HcStatus] = field(default_factory=dict)
    turn: int = 0
    reported: bool = False
    title: str = ""
    parent: str | None = None
    foreground: HcForeground | None = None


@dataclass(frozen=True)
class HcSubscribeResult:
    seq: int
    terminals: tuple[HcTerminal, ...]
    channel: int | None


@dataclass(frozen=True)
class HcEventNotification:
    seq: int
    kind: str
    terminal: str | None
    machine: str | None
    data: Any


@dataclass(frozen=True)
class HcHeartbeatNotification:
    seq: int


@dataclass(frozen=True)
class HcMissedNotification:
    seq: int
    terminals: tuple[HcTerminal, ...]


@dataclass(frozen=True)
class HcUnknownNotification:
    method: str
    params: Any


HcNotification = (
    HcEventNotification
    | HcHeartbeatNotification
    | HcMissedNotification
    | HcUnknownNotification
)


@dataclass(frozen=True)
class HcSchemaReport:
    cli_version: str | None
    schema_version: str | None
    openrpc_version: str | None
    methods: frozenset[str]
    missing_methods: tuple[str, ...]
    missing_params: Mapping[str, tuple[str, ...]]

    @property
    def compatible(self) -> bool:
        return not self.missing_methods and not self.missing_params

    def as_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "expected_protocol_generation": PROTOCOL_GENERATION,
            "cli_version": self.cli_version,
            "schema_version": self.schema_version,
            "openrpc_version": self.openrpc_version,
            "methods": sorted(self.methods),
            "missing_methods": list(self.missing_methods),
            "missing_params": {
                method: list(params) for method, params in self.missing_params.items()
            },
        }


def parse_hello_result(value: Any, *, require_plain: bool = True) -> HcHello:
    item = _object(value, "hello result")
    generation = _integer(item, "generation")
    version = _string(item, "version")
    core_item = _object(item.get("core"), "hello core")
    core = HcCoreInfo(id=_string(core_item, "id"), name=_string(core_item, "name"))
    framed = _boolean(item, "framed")
    methods = frozenset(_string_list(item.get("methods"), "hello methods"))
    features = frozenset(
        _string_list(item.get("features", []), "hello features")
    )

    if generation != PROTOCOL_GENERATION:
        raise HcProtocolError(
            f"unsupported Core protocol generation {generation}; "
            f"expected {PROTOCOL_GENERATION}"
        )
    missing = sorted(REQUIRED_METHODS - methods)
    if missing:
        raise HcProtocolError(f"Core is missing required methods: {', '.join(missing)}")
    if require_plain and framed:
        raise HcProtocolError("plain-mode handshake unexpectedly entered framed mode")

    return HcHello(
        generation=generation,
        version=version,
        core=core,
        framed=framed,
        methods=methods,
        features=features,
    )


def parse_terminal(value: Any) -> HcTerminal:
    item = _object(value, "terminal")
    labels_item = _object(item.get("labels"), "terminal labels")
    labels = {
        _string_value(key, "terminal label key"): _string_value(
            label, f"terminal label {key}"
        )
        for key, label in labels_item.items()
    }
    status_value = item.get("status")
    foreground_value = item.get("foreground")

    return HcTerminal(
        address=_string(item, "address"),
        labels=labels,
        argv=tuple(_string_list(item.get("argv"), "terminal argv")),
        cwd=_string(item, "cwd"),
        state=_string(item, "state"),
        created_ms=_integer(item, "created_ms"),
        screen=_string(item, "screen"),
        cols=_integer(item, "cols"),
        rows=_integer(item, "rows"),
        name=_optional_string(item, "name"),
        pid=_optional_integer(item, "pid"),
        exit_code=_optional_integer(item, "exit_code"),
        signal=_optional_integer(item, "signal"),
        reason=_optional_string(item, "reason"),
        finished_ms=_optional_integer(item, "finished_ms"),
        status=None if status_value is None else parse_status(status_value),
        status_ms=_optional_integer(item, "status_ms"),
        records=_parse_status_records(item.get("records", {})),
        turn=_integer(item, "turn", default=0),
        reported=_boolean(item, "reported", default=False),
        title=_string(item, "title", default=""),
        parent=_optional_string(item, "parent"),
        foreground=(
            None if foreground_value is None else parse_foreground(foreground_value)
        ),
    )


def parse_status(value: Any) -> HcStatus:
    item = _object(value, "status")
    progress = _optional_integer(item, "progress")
    if progress is not None and not 0 <= progress <= 255:
        raise HcProtocolError("status progress must be between 0 and 255")
    return HcStatus(
        state=_string(item, "state"),
        app=_string(item, "app", default=""),
        title=_string(item, "title", default=""),
        message=_string(item, "msg", default=""),
        kind=_optional_string(item, "kind"),
        progress=progress,
    )


def parse_foreground(value: Any) -> HcForeground:
    item = _object(value, "foreground")
    return HcForeground(
        pid=_integer(item, "pid"),
        name=_string(item, "name"),
        argv=tuple(_string_list(item.get("argv"), "foreground argv")),
        cwd=_string(item, "cwd"),
    )


def parse_subscribe_result(value: Any) -> HcSubscribeResult:
    item = _object(value, "subscribe result")
    terminals = item.get("terminals")
    if not isinstance(terminals, list):
        raise HcProtocolError("subscribe result terminals must be an array")
    return HcSubscribeResult(
        seq=_integer(item, "seq"),
        terminals=tuple(parse_terminal(terminal) for terminal in terminals),
        channel=_optional_integer(item, "channel"),
    )


def decode_notification_line(line: bytes | str) -> HcNotification:
    raw = line.encode() if isinstance(line, str) else line
    if len(raw) > MAX_JSON_LINE_BYTES:
        raise HcProtocolError(
            f"Core JSON line exceeds {MAX_JSON_LINE_BYTES} bytes"
        )
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HcProtocolError(f"invalid Core notification JSON: {exc}") from exc
    return parse_notification(value)


def parse_notification(value: Any) -> HcNotification:
    item = _object(value, "notification")
    if item.get("jsonrpc") not in (None, "2.0"):
        raise HcProtocolError("notification jsonrpc must be '2.0' when present")
    method = _string(item, "method")
    params = item.get("params", {})

    if method == "event":
        details = _object(params, "event params")
        return HcEventNotification(
            seq=_integer(details, "seq"),
            kind=_string(details, "type"),
            terminal=_optional_string(details, "terminal"),
            machine=_optional_string(details, "machine"),
            data=details.get("data", {}),
        )
    if method == "heartbeat":
        details = _object(params, "heartbeat params")
        return HcHeartbeatNotification(seq=_integer(details, "seq"))
    if method == "missed":
        details = _object(params, "missed params")
        terminals = details.get("terminals")
        if not isinstance(terminals, list):
            raise HcProtocolError("missed params terminals must be an array")
        return HcMissedNotification(
            seq=_integer(details, "seq"),
            terminals=tuple(parse_terminal(terminal) for terminal in terminals),
        )
    return HcUnknownNotification(method=method, params=params)


def inspect_openrpc_schema(
    value: Any, *, cli_version: str | None = None
) -> HcSchemaReport:
    document = _object(value, "OpenRPC document")
    method_items = document.get("methods")
    if not isinstance(method_items, list):
        raise HcProtocolError("OpenRPC methods must be an array")

    params_by_method: dict[str, frozenset[str]] = {}
    for raw_method in method_items:
        method = _object(raw_method, "OpenRPC method")
        name = _string(method, "name")
        raw_params = method.get("params", [])
        if not isinstance(raw_params, list):
            raise HcProtocolError(f"OpenRPC params for {name} must be an array")
        params_by_method[name] = frozenset(
            _string(_object(param, f"OpenRPC parameter for {name}"), "name")
            for param in raw_params
        )

    methods = frozenset(params_by_method)
    missing_methods = tuple(sorted(REQUIRED_METHODS - methods))
    missing_params: dict[str, tuple[str, ...]] = {}
    for method, required in REQUIRED_METHOD_PARAMS.items():
        if method not in params_by_method:
            continue
        missing = tuple(sorted(required - params_by_method[method]))
        if missing:
            missing_params[method] = missing

    info = document.get("info", {})
    if not isinstance(info, dict):
        raise HcProtocolError("OpenRPC info must be an object")
    schema_version = info.get("version")
    if schema_version is not None and not isinstance(schema_version, str):
        raise HcProtocolError("OpenRPC info.version must be a string")
    openrpc_version = document.get("openrpc")
    if openrpc_version is not None and not isinstance(openrpc_version, str):
        raise HcProtocolError("OpenRPC openrpc must be a string")

    return HcSchemaReport(
        cli_version=cli_version,
        schema_version=schema_version,
        openrpc_version=openrpc_version,
        methods=methods,
        missing_methods=missing_methods,
        missing_params=missing_params,
    )


def _parse_status_records(value: Any) -> dict[str, HcStatus]:
    records = _object(value, "terminal records")
    return {
        _string_value(key, "terminal record key"): parse_status(status)
        for key, status in records.items()
    }


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HcProtocolError(f"{label} must be an object")
    return value


def _string(item: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    if key not in item and default is not None:
        return default
    return _string_value(item.get(key), key)


def _string_value(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise HcProtocolError(f"{label} must be a string")
    return value


def _optional_string(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    return _string_value(value, key)


def _integer(
    item: Mapping[str, Any], key: str, *, default: int | None = None
) -> int:
    if key not in item and default is not None:
        return default
    return _integer_value(item.get(key), key)


def _integer_value(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HcProtocolError(f"{label} must be an integer")
    if value < 0 and label not in {"pid", "exit_code", "signal"}:
        raise HcProtocolError(f"{label} must not be negative")
    return value


def _optional_integer(item: Mapping[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None:
        return None
    return _integer_value(value, key)


def _boolean(
    item: Mapping[str, Any], key: str, *, default: bool | None = None
) -> bool:
    if key not in item and default is not None:
        return default
    value = item.get(key)
    if not isinstance(value, bool):
        raise HcProtocolError(f"{key} must be a boolean")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise HcProtocolError(f"{label} must be an array of strings")
    return value
