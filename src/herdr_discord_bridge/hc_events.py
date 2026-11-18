from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from .config import HcRuntimeConfig
from .hc_protocol import (
    MAX_JSON_LINE_BYTES,
    PROTOCOL_GENERATION,
    HcEventNotification,
    HcHeartbeatNotification,
    HcHello,
    HcMissedNotification,
    HcNotification,
    HcProtocolError,
    HcSubscribeResult,
    HcTerminal,
    HcUnknownNotification,
    decode_notification_line,
    parse_hello_result,
    parse_status,
    parse_subscribe_result,
    parse_terminal,
)
from .hc_rpc import (
    HcRpcError,
    HcSocketLocator,
    decode_response_line,
    encode_request,
)


@dataclass(frozen=True, slots=True)
class HcStatusTransition:
    terminal: str
    previous_state: str | None
    current_state: str
    app: str | None
    resynchronized: bool = False


class HcTerminalState:
    """Apply ordered Core notifications to the latest terminal snapshot."""

    def __init__(self, snapshot: HcSubscribeResult):
        self.seq = snapshot.seq
        self.terminals = {terminal.address: terminal for terminal in snapshot.terminals}

    def apply(self, notification: HcNotification) -> tuple[HcStatusTransition, ...]:
        if isinstance(notification, HcUnknownNotification):
            return ()
        if notification.seq < self.seq:
            raise HcProtocolError(
                f"Core event sequence moved backwards from {self.seq} to {notification.seq}"
            )
        self.seq = notification.seq
        if isinstance(notification, HcHeartbeatNotification):
            return ()
        if isinstance(notification, HcMissedNotification):
            return self._replace_snapshot(notification.terminals)
        return self._apply_event(notification)

    def _replace_snapshot(
        self, terminals: tuple[HcTerminal, ...]
    ) -> tuple[HcStatusTransition, ...]:
        previous = self.terminals
        self.terminals = {terminal.address: terminal for terminal in terminals}
        transitions = []
        for address, terminal in self.terminals.items():
            old = previous.get(address)
            transition = _status_transition(old, terminal, resynchronized=True)
            if transition is not None:
                transitions.append(transition)
        return tuple(transitions)

    def _apply_event(
        self, notification: HcEventNotification
    ) -> tuple[HcStatusTransition, ...]:
        address = notification.terminal
        if not address:
            return ()
        kind = notification.kind
        data = notification.data
        if kind in {"created", "restarted"}:
            terminal = parse_terminal(data)
            if terminal.address != address:
                raise HcProtocolError(
                    f"Core event terminal {address!r} contained {terminal.address!r}"
                )
            self.terminals[address] = terminal
            return ()
        if kind == "removed":
            self.terminals.pop(address, None)
            return ()

        terminal = self.terminals.get(address)
        if terminal is None:
            raise HcProtocolError(
                f"Core event {kind!r} referenced unknown terminal {address!r}"
            )
        if not isinstance(data, dict):
            raise HcProtocolError(f"Core event {kind!r} data must be an object")

        updated = terminal
        if kind == "status":
            raw_status = data.get("status")
            status = None if raw_status is None else parse_status(raw_status)
            reported = data.get("reported", terminal.reported)
            if not isinstance(reported, bool):
                raise HcProtocolError("status event reported must be a boolean")
            updated = replace(terminal, status=status, reported=reported)
        elif kind == "turn":
            turn = data.get("turn", terminal.turn)
            if not isinstance(turn, int) or isinstance(turn, bool):
                raise HcProtocolError("turn event turn must be an integer")
            status = terminal.status
            state = data.get("state")
            if state is not None:
                if not isinstance(state, str):
                    raise HcProtocolError("turn event state must be a string")
                if status is not None:
                    status = replace(status, state=state)
            updated = replace(terminal, turn=turn, status=status)
        elif kind == "labels":
            labels = data.get("labels")
            if not isinstance(labels, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in labels.items()
            ):
                raise HcProtocolError("labels event labels must be a string mapping")
            updated = replace(terminal, labels=dict(labels))
        elif kind == "title":
            title = data.get("title")
            if not isinstance(title, str):
                raise HcProtocolError("title event title must be a string")
            updated = replace(terminal, title=title)
        elif kind == "finished":
            updated = replace(
                terminal,
                state="finished",
                finished_ms=_optional_int(data, "finished_ms", terminal.finished_ms),
                exit_code=_optional_int(data, "exit_code", terminal.exit_code),
                signal=_optional_int(data, "signal", terminal.signal),
                reason=_optional_str(data, "reason", terminal.reason),
            )
        elif kind in {"size", "doc", "keep", "lost"}:
            return ()
        else:
            return ()

        self.terminals[address] = updated
        transition = _status_transition(terminal, updated)
        return (transition,) if transition is not None else ()


class HcEventSubscription:
    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        hello: HcHello,
        snapshot: HcSubscribeResult,
        event_idle_timeout_seconds: float,
    ):
        self.reader = reader
        self.writer = writer
        self.hello = hello
        self.snapshot = snapshot
        self.event_idle_timeout_seconds = event_idle_timeout_seconds

    async def next_notification(
        self, *, timeout: float | None = None
    ) -> HcNotification:
        line = await _read_line(
            self.reader,
            timeout=self.event_idle_timeout_seconds if timeout is None else timeout,
        )
        try:
            return decode_notification_line(line)
        except HcProtocolError as exc:
            raise HcRpcError(str(exc)) from exc

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class HcEventStream:
    """Open a persistent subscription on an already-running local Core."""

    def __init__(self, config: HcRuntimeConfig):
        self.config = config
        self.locator = HcSocketLocator(config)

    async def connect(
        self,
        *,
        terminal: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> HcEventSubscription:
        path = self.locator.resolve()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    str(path), limit=MAX_JSON_LINE_BYTES + 1
                ),
                timeout=self.config.connect_timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise HcRpcError(
                f"cannot connect to existing Herdr Core socket {path}: {exc}"
            ) from exc

        try:
            writer.write(
                encode_request(
                    request_id="herdrelay-events-hello",
                    method="hello",
                    params={"generation": PROTOCOL_GENERATION, "framed": False},
                )
            )
            await writer.drain()
            hello_result = decode_response_line(
                await _read_line(
                    reader, timeout=self.config.request_timeout_seconds
                ),
                expected_id="herdrelay-events-hello",
            )
            hello = parse_hello_result(hello_result)

            params: dict[str, Any] = {"labels": labels or {}}
            if terminal is not None:
                params["terminal"] = terminal
            writer.write(
                encode_request(
                    request_id="herdrelay-events-subscribe",
                    method="events.subscribe",
                    params=params,
                )
            )
            await writer.drain()
            subscribe_result = decode_response_line(
                await _read_line(
                    reader, timeout=self.config.request_timeout_seconds
                ),
                expected_id="herdrelay-events-subscribe",
            )
            snapshot = parse_subscribe_result(subscribe_result)
            return HcEventSubscription(
                reader=reader,
                writer=writer,
                hello=hello,
                snapshot=snapshot,
                event_idle_timeout_seconds=self.config.event_idle_timeout_seconds,
            )
        except (HcProtocolError, HcRpcError, TimeoutError, OSError) as exc:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if isinstance(exc, HcRpcError):
                raise
            raise HcRpcError(str(exc)) from exc


async def _read_line(reader: asyncio.StreamReader, *, timeout: float) -> bytes:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except (TimeoutError, ValueError, asyncio.LimitOverrunError) as exc:
        raise HcRpcError(f"Core event stream read failed: {exc}") from exc
    if not line:
        raise HcRpcError("Core event stream closed")
    if len(line) > MAX_JSON_LINE_BYTES + 1:
        raise HcRpcError(f"Core JSON line exceeds {MAX_JSON_LINE_BYTES} bytes")
    return line.rstrip(b"\n")


def _status_transition(
    previous: HcTerminal | None,
    current: HcTerminal,
    *,
    resynchronized: bool = False,
) -> HcStatusTransition | None:
    previous_state = previous.status.state if previous and previous.status else None
    current_state = current.status.state if current.status else None
    if current_state is None or current_state == previous_state:
        return None
    return HcStatusTransition(
        terminal=current.address,
        previous_state=previous_state,
        current_state=current_state,
        app=current.status.app or None,
        resynchronized=resynchronized,
    )


def _optional_int(data: dict[str, Any], key: str, default: int | None) -> int | None:
    value = data.get(key, default)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise HcProtocolError(f"{key} must be an integer or null")
    return value


def _optional_str(data: dict[str, Any], key: str, default: str | None) -> str | None:
    value = data.get(key, default)
    if value is not None and not isinstance(value, str):
        raise HcProtocolError(f"{key} must be a string or null")
    return value
