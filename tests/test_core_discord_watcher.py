from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from herdrelay.config import (
    AppConfig,
    HcRuntimeConfig,
    RuntimeConfig,
    WatcherConfig,
)
from herdrelay.hc_client import HcClient
from herdrelay.hc_events import HcTerminalState
from herdrelay.hc_protocol import (
    decode_notification_line,
    parse_hello_result,
    parse_subscribe_result,
)
from herdrelay.core_discord_watcher import CoreEventWatcher, core_status_events

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/hc"
HELLO = parse_hello_result(json.loads((FIXTURES / "hello.json").read_text()))
SUBSCRIBE = parse_subscribe_result(
    json.loads((FIXTURES / "subscribe.json").read_text())
)
NOTIFICATIONS = [
    decode_notification_line(line)
    for line in (FIXTURES / "notifications.ndjson").read_bytes().splitlines()
]


def core_config(*addresses: str, statuses=("blocked", "done")) -> AppConfig:
    return AppConfig(
        discord_token="token",
        runtime=RuntimeConfig(
            provider="herdr-core",
            core=HcRuntimeConfig(
                allowed_terminal_addresses=frozenset(addresses)
            ),
        ),
        enable_watcher=True,
        watcher=WatcherConfig(statuses=statuses),
    )


def test_core_status_events_apply_allowlist_visibility_and_status_filters():
    client = HcClient(core_config("t12", "t14", "remote/t7"))
    state = HcTerminalState(SUBSCRIBE)

    blocked_transition = state.apply(NOTIFICATIONS[0])
    blocked = core_status_events(
        client, state, blocked_transition, statuses=("blocked",)
    )

    assert len(blocked) == 1
    assert blocked[0].pane_id == "t12"
    assert blocked[0].agent_name == "pi"
    assert blocked[0].previous_status == "working"
    assert blocked[0].raw["state_change_seq"] == 41

    worker_state = HcTerminalState(SUBSCRIBE)
    worker_event = decode_notification_line(
        '{"jsonrpc":"2.0","method":"event","params":'
        '{"seq":41,"type":"status","terminal":"t14","data":'
        '{"status":{"state":"done","app":"pi"},"reported":true}}}'
    )
    worker_transition = worker_state.apply(worker_event)
    assert core_status_events(
        client, worker_state, worker_transition, statuses=("done",)
    ) == ()

    assert core_status_events(
        client, state, blocked_transition, statuses=("done",)
    ) == ()


def test_core_status_events_mark_missed_snapshot_resynchronization():
    client = HcClient(core_config("t12"))
    state = HcTerminalState(SUBSCRIBE)
    state.apply(NOTIFICATIONS[0])

    transitions = state.apply(NOTIFICATIONS[9])
    events = core_status_events(client, state, transitions, statuses=("done",))

    assert len(events) == 1
    assert events[0].status == "done"
    assert events[0].raw["resynchronized"] is True
    assert events[0].raw["state_change_seq"] == 50


class FakeSubscription:
    def __init__(self):
        self.hello = HELLO
        self.snapshot = SUBSCRIBE
        self.notifications = [NOTIFICATIONS[0]]
        self.closed = False

    async def next_notification(self):
        if self.notifications:
            return self.notifications.pop(0)
        await asyncio.Future()

    async def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, subscription):
        self.subscription = subscription

    async def connect(self):
        return self.subscription


class RecordingCoreWatcher(CoreEventWatcher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.events = []

    async def _notify(self, event):
        self.events.append(event)
        self.stop()


@pytest.mark.asyncio
async def test_core_watcher_observes_identity_and_delivers_transition():
    config = core_config("t12", statuses=("blocked",))
    client = HcClient(config)
    subscription = FakeSubscription()
    watcher = RecordingCoreWatcher(
        bot=None,
        config=config,
        store=None,
        client=client,
        stream_factory=lambda: FakeStream(subscription),
    )

    await watcher._run_once()

    assert client.core_id == "core-test-001"
    assert [event.status for event in watcher.events] == ["blocked"]
    assert subscription.closed is True
