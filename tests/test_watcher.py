import asyncio
import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest

from herdr_discord_bridge.config import AppConfig, WatcherConfig
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.watcher import (
    AgentStatusEvent,
    EventWatcher,
    blocked_mention_prefix,
    build_agent_status_subscriptions,
    event_dedupe_key,
    event_state_marker,
    parse_agent_status_event,
    resolve_socket_path,
    should_resubscribe,
)


def test_parse_agent_status_event_from_nested_event_payload():
    event = parse_agent_status_event(
        {
            "event": {
                "type": "pane.agent_status_changed",
                "payload": {
                    "pane_id": "w7:p2",
                    "new_status": "BLOCKED",
                    "previous_status": "working",
                    "agent_name": "codex",
                },
            }
        }
    )

    assert event == AgentStatusEvent(
        pane_id="w7:p2",
        status="blocked",
        previous_status="working",
        agent_name="codex",
        raw={
            "event": {
                "type": "pane.agent_status_changed",
                "payload": {
                    "pane_id": "w7:p2",
                    "new_status": "BLOCKED",
                    "previous_status": "working",
                    "agent_name": "codex",
                },
            }
        },
    )


def test_parse_agent_status_event_from_pane_snapshot():
    event = parse_agent_status_event(
        {
            "type": "pane.agent_status_changed",
            "pane": {"pane_id": "w1:p1", "agent_status": "done"},
            "agent": {"name": "claude"},
        }
    )

    assert event is not None
    assert event.pane_id == "w1:p1"
    assert event.status == "done"
    assert event.agent_name == "claude"


def test_parse_agent_status_event_herdr_095_wire_format():
    event = parse_agent_status_event({
        "data": {"agent": "pi", "agent_status": "blocked", "pane_id": "w1:p5", "workspace_id": "w1"},
        "event": "pane.agent_status_changed",
    })

    assert event is not None
    assert (event.pane_id, event.status, event.agent_name) == ("w1:p5", "blocked", "pi")


def test_parse_agent_status_event_ignores_other_events():
    assert parse_agent_status_event({"type": "pane.created", "pane_id": "w1:p1"}) is None


def test_dedupe_key_is_stable_across_time():
    event = AgentStatusEvent(pane_id="w1:p1", status="blocked")

    first = event_dedupe_key(event, "tail")
    second = event_dedupe_key(event, "tail")

    assert first == second
    assert first == "w1:p1:blocked:0c62f876ef1dea83"


def test_event_state_marker_uses_state_change_sequence_without_reading_output():
    event = AgentStatusEvent(pane_id="w1:p1", status="blocked")

    marker = event_state_marker(StateMarkerClient(), event)

    assert marker == "state-change:42"


def test_find_binding_for_target_prefers_thread_binding(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=None,
        herdr_target="w1:p1",
        label="channel",
        created_by=10,
    )
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=3,
        herdr_target="w1:p1",
        label="thread",
        created_by=10,
    )

    binding = store.find_binding_for_target("w1:p1")

    assert binding is not None
    assert binding.thread_id == "3"


def test_resolve_socket_path_uses_config_first():
    assert resolve_socket_path(AppConfig(discord_token="token", herdr_socket_path="/tmp/herdr.sock")) == (
        "/tmp/herdr.sock"
    )


def test_build_agent_status_subscriptions_uses_pane_and_status_filters():
    config = AppConfig(discord_token="token")

    subscriptions = build_agent_status_subscriptions(FakeHerdrClient(), config)

    assert subscriptions == [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1", "agent_status": "blocked"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1", "agent_status": "done"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p2", "agent_status": "blocked"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p2", "agent_status": "done"},
    ]


def test_should_resubscribe_after_configured_interval(monkeypatch):
    config = AppConfig(discord_token="token")
    monkeypatch.setattr("herdr_discord_bridge.watcher.time.monotonic", lambda: 500.0)

    assert should_resubscribe(199.9, config)
    assert not should_resubscribe(250.1, config)


def test_should_resubscribe_can_be_disabled(monkeypatch):
    config = AppConfig(
        discord_token="token",
        watcher=WatcherConfig(
            resubscribe_interval_seconds=0,
        ),
    )
    monkeypatch.setattr("herdr_discord_bridge.watcher.time.monotonic", lambda: 1000.0)

    assert not should_resubscribe(0, config)


def test_blocked_mention_prefix_lists_allowed_users():
    config = AppConfig(discord_token="token", allowed_user_ids=frozenset({111, 222}))

    prefix = blocked_mention_prefix(config)

    assert "<@111>" in prefix
    assert "<@222>" in prefix
    assert prefix.endswith("\n")


def test_blocked_mention_prefix_empty_when_no_users():
    config = AppConfig(discord_token="token")

    assert blocked_mention_prefix(config) == ""


def test_build_subscriptions_covers_shells_before_agent_detection():
    client = Mock()
    client.list_panes.return_value = [HerdrTarget(target="w1:p1", agent_name=None)]
    config = AppConfig(discord_token="token", watcher=WatcherConfig(statuses=("blocked",)))

    assert build_agent_status_subscriptions(client, config) == [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1", "agent_status": "blocked"},
    ]
    client.list_targets.assert_not_called()


def test_build_subscriptions_with_no_panes_is_not_an_error():
    client = Mock()
    client.list_panes.return_value = []

    assert build_agent_status_subscriptions(client, AppConfig(discord_token="token")) == []


@pytest.mark.asyncio
async def test_empty_watcher_waits_then_subscribes_when_a_pane_appears(monkeypatch, caplog):
    client = Mock()
    client.list_panes.return_value = []
    watcher = EventWatcher(
        bot=None, config=AppConfig(discord_token="token"), store=None, client=client,
    )
    watcher._sleep_reconnect = AsyncMock()
    connection = AsyncMock()
    monkeypatch.setattr(asyncio, "open_unix_connection", connection)
    caplog.set_level(logging.INFO)

    await watcher._run_once()
    await watcher._run_once()

    assert watcher._sleep_reconnect.await_count == 2
    connection.assert_not_awaited()
    assert caplog.text.count("No Herdr panes to watch") == 1
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)

    client.list_panes.return_value = [HerdrTarget(target="w1:p1")]
    reader = Mock(readline=AsyncMock(return_value=b'{"id":"discord_bridge_events"}\n'))
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    connection.return_value = (reader, writer)
    async def handle_line(line):
        watcher.stop()
    watcher._handle_line = handle_line

    await watcher._run_once()

    connection.assert_awaited_once()
    request = json.loads(writer.write.call_args.args[0])
    assert request["method"] == "events.subscribe"
    assert {s["pane_id"] for s in request["params"]["subscriptions"] if "pane_id" in s} == {"w1:p1"}
    assert {s["type"] for s in request["params"]["subscriptions"] if "pane_id" not in s} == {
        "pane.created", "pane.closed", "pane.moved",
    }
    assert not watcher._waiting_for_panes
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_watcher_can_stop_without_waiting_full_reconnect_delay():
    client = Mock()
    client.list_panes.return_value = []
    watcher = EventWatcher(
        bot=None,
        config=AppConfig(discord_token="token", watcher=WatcherConfig(reconnect_delay_seconds=60)),
        store=None, client=client,
    )
    watcher.stop()

    await asyncio.wait_for(watcher._run_once(), timeout=1)


def _event_line(event):
    return (json.dumps(event) + "\n").encode()


def _socket_pair(*events):
    reader = Mock(readline=AsyncMock(side_effect=[_event_line(event) for event in events]))
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    return reader, writer


def _watcher(client):
    return EventWatcher(
        bot=None, store=None, client=client,
        config=AppConfig(discord_token="token", watcher=WatcherConfig(
            statuses=("blocked",), resubscribe_interval_seconds=0,
        )),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [
    "pane.created", "pane.closed", "pane.moved",
    "pane_created", "pane_closed", "pane_moved",
])
@pytest.mark.parametrize("nested", [False, True])
async def test_topology_events_request_refresh_without_notifying_discord(event_type, nested):
    watcher = _watcher(Mock())
    watcher._notify = AsyncMock()
    event = {"event": event_type, "data": {"pane_id": "w1:p2"}}
    if nested:
        event = {"event": {"type": event_type, "payload": {"pane_id": "w1:p2"}}}

    assert await watcher._handle_line(_event_line(event)) == "topology_changed"
    watcher._notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_style", ["dotted", "snake_case"])
@pytest.mark.parametrize("event_type, old_ids, new_ids", [
    ("pane.created", ["w1:p1"], ["w1:p1", "w1:p2"]),
    ("pane.closed", ["w1:p1", "w1:p2"], ["w1:p2"]),
    ("pane.moved", ["w1:p1"], ["w2:p1"]),
])
async def test_topology_refreshes_filters_with_periodic_reconnect_disabled(
    monkeypatch, event_type, old_ids, new_ids, wire_style,
):
    before = [HerdrTarget(target=pane_id) for pane_id in old_ids]
    after = [HerdrTarget(target=pane_id) for pane_id in new_ids]
    client = Mock()
    client.list_panes.side_effect = [before, before, after, after]
    watcher = _watcher(client)
    ack = {"id": "discord_bridge_events", "result": {}}
    wire_type = event_type.replace(".", "_") if wire_style == "snake_case" else event_type
    first = _socket_pair(ack, {"event": wire_type, "data": {"type": wire_type}})
    second = _socket_pair(ack, {
        "event": "pane.agent_status_changed",
        "data": {"pane_id": new_ids[-1], "agent_status": "blocked", "agent": "pi"},
    })
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    notifications = []
    async def notify(event):
        notifications.append(event)
        watcher.stop()
    watcher._notify = notify

    await watcher._run_once()
    await watcher._run_once()

    assert connect.await_count == 2
    for pair, expected_ids in [(first, old_ids), (second, new_ids)]:
        writer = pair[1]
        subscriptions = json.loads(writer.write.call_args.args[0])["params"]["subscriptions"]
        assert {s["pane_id"] for s in subscriptions if "pane_id" in s} == set(expected_ids)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
    assert len(notifications) == 1
    assert notifications[0].pane_id == new_ids[-1]
    assert notifications[0].status == "blocked"


@pytest.mark.asyncio
async def test_post_ack_snapshot_closes_discovery_subscription_gap(monkeypatch):
    client = Mock()
    client.list_panes.side_effect = [
        [HerdrTarget(target="w1:p1")],
        [HerdrTarget(target="w1:p1"), HerdrTarget(target="w1:p2")],
    ]
    watcher = _watcher(client)
    reader, writer = _socket_pair({"id": "discord_bridge_events", "result": {}})
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)))

    await watcher._run_once()

    assert client.list_panes.call_count == 2
    reader.readline.assert_awaited_once()
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_events_lost_error_closes_socket_for_retry(monkeypatch):
    client = Mock()
    client.list_panes.return_value = [HerdrTarget(target="w1:p1")]
    watcher = _watcher(client)
    reader, writer = _socket_pair(
        {"id": "discord_bridge_events", "result": {}},
        {"error": {"code": "events_lost", "message": "subscriber fell behind"}},
    )
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)))

    with pytest.raises(RuntimeError, match="events_lost"):
        await watcher._run_once()

    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


class StateMarkerClient:
    def resolve_target(self, target):
        return HerdrTarget(target=target, raw={"state_change_seq": 42})


class FakeHerdrClient:
    def list_panes(self):
        return [
            HerdrTarget(target="w1:p1"),
            HerdrTarget(target="w1:p2"),
        ]
