from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from herdrelay.config import HcRuntimeConfig
from herdrelay.hc_events import HcEventStream, HcTerminalState
from herdrelay.hc_protocol import (
    HcHeartbeatNotification,
    HcProtocolError,
    decode_notification_line,
    parse_subscribe_result,
)
from herdrelay.hc_rpc import HcRpcError

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/hc"
HELLO = json.loads((FIXTURES / "hello.json").read_text())
SUBSCRIBE = json.loads((FIXTURES / "subscribe.json").read_text())
NOTIFICATIONS = [
    decode_notification_line(line)
    for line in (FIXTURES / "notifications.ndjson").read_bytes().splitlines()
]


@pytest.fixture
def short_socket_path():
    directory = Path(tempfile.mkdtemp(prefix="hevents.", dir="/tmp"))
    try:
        yield directory / "s"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_event_stream_handshakes_subscribes_and_reads_notifications(
    short_socket_path,
):
    requests = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        hello_request = json.loads(await reader.readline())
        requests.append(hello_request)
        writer.write(_response(hello_request["id"], HELLO))
        await writer.drain()

        subscribe_request = json.loads(await reader.readline())
        requests.append(subscribe_request)
        writer.write(_response(subscribe_request["id"], SUBSCRIBE))
        writer.write(
            b'{"jsonrpc":"2.0","method":"heartbeat","params":{"seq":40}}\n'
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(short_socket_path))
    try:
        subscription = await HcEventStream(
            HcRuntimeConfig(socket_path=str(short_socket_path))
        ).connect(labels={"herdr.space": "smoke"})
        notification = await subscription.next_notification()
        await subscription.close()
    finally:
        server.close()
        await server.wait_closed()

    assert subscription.hello.core.id == "core-test-001"
    assert subscription.snapshot.seq == 40
    assert isinstance(notification, HcHeartbeatNotification)
    assert requests[0]["method"] == "hello"
    assert requests[1]["method"] == "events.subscribe"
    assert requests[1]["params"] == {"labels": {"herdr.space": "smoke"}}


@pytest.mark.asyncio
async def test_event_stream_missing_socket_does_not_start_core(short_socket_path):
    stream = HcEventStream(HcRuntimeConfig(socket_path=str(short_socket_path)))

    with pytest.raises(HcRpcError, match="cannot connect to existing"):
        await stream.connect()

    assert not short_socket_path.exists()


def test_terminal_state_applies_status_turn_labels_and_lifecycle():
    state = HcTerminalState(parse_subscribe_result(SUBSCRIBE))

    blocked = state.apply(NOTIFICATIONS[0])
    assert len(blocked) == 1
    assert blocked[0].terminal == "t12"
    assert blocked[0].previous_state == "working"
    assert blocked[0].current_state == "blocked"

    done = state.apply(NOTIFICATIONS[1])
    assert len(done) == 1
    assert done[0].previous_state == "blocked"
    assert done[0].current_state == "done"
    assert state.terminals["t12"].turn == 4

    state.apply(NOTIFICATIONS[2])
    assert state.terminals["t17"].status is not None
    state.apply(NOTIFICATIONS[3])
    assert state.terminals["t17"].reported is False
    state.apply(NOTIFICATIONS[4])
    assert state.terminals["t12"].labels["herdr.tab"] == "tab-renamed"
    state.apply(NOTIFICATIONS[5])
    assert state.terminals["t12"].title == "π - Core migration"
    state.apply(NOTIFICATIONS[6])
    assert state.terminals["t17"].state == "finished"
    state.apply(NOTIFICATIONS[7])
    assert "t17" not in state.terminals
    assert state.apply(NOTIFICATIONS[8]) == ()


def test_terminal_state_missed_replaces_snapshot_and_reports_changed_status():
    state = HcTerminalState(parse_subscribe_result(SUBSCRIBE))
    state.apply(NOTIFICATIONS[0])

    transitions = state.apply(NOTIFICATIONS[9])

    assert state.seq == 50
    assert list(state.terminals) == ["t12"]
    assert len(transitions) == 1
    assert transitions[0].previous_state == "blocked"
    assert transitions[0].current_state == "done"
    assert transitions[0].resynchronized is True


def test_terminal_state_tolerates_future_notification_and_event_kind():
    state = HcTerminalState(parse_subscribe_result(SUBSCRIBE))

    assert state.apply(NOTIFICATIONS[10]) == ()
    future = decode_notification_line(
        '{"jsonrpc":"2.0","method":"event","params":'
        '{"seq":41,"type":"future","terminal":"t12","data":{"x":1}}}'
    )
    assert state.apply(future) == ()
    assert state.seq == 41


def test_terminal_state_rejects_backwards_sequence_and_unknown_terminal():
    state = HcTerminalState(parse_subscribe_result(SUBSCRIBE))
    backwards = decode_notification_line(
        '{"jsonrpc":"2.0","method":"heartbeat","params":{"seq":39}}'
    )
    with pytest.raises(HcProtocolError, match="moved backwards"):
        state.apply(backwards)

    unknown = decode_notification_line(
        '{"jsonrpc":"2.0","method":"event","params":'
        '{"seq":41,"type":"status","terminal":"t99","data":'
        '{"status":{"state":"idle","app":"pi"},"reported":true}}}'
    )
    with pytest.raises(HcProtocolError, match="unknown terminal"):
        state.apply(unknown)


def _response(request_id: str, result: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result}
    ).encode() + b"\n"
