from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from herdrelay.config import AppConfig, StreamConfig, TelegramConfig
from herdrelay.domain import TargetKey
from herdrelay.store import Store
from herdrelay.discord_output import StreamManager
from herdrelay.telegram_output import TelegramOutputRelay


@dataclass
class FakeApi:
    sent: list[dict] = field(default_factory=list)

    async def send_text(self, **kwargs):
        self.sent.append(kwargs)
        return ()


def write_v2(path: Path, **fields) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "event_id": "event-1",
                "runtime": "herdr-core",
                "core_id": "core-1",
                "target": "t12",
                "agent": "pi",
                "text": "completed response",
                **fields,
            }
        )
    )


def write_v1(path: Path, **fields) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "legacy-event-1",
                "pane_id": "w1:p1",
                "agent": "pi",
                "text": "legacy response",
                **fields,
            }
        )
    )


def relay(tmp_path, *, max_age=86_400):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    config = AppConfig(
        discord_token="",
        enable_streaming=True,
        streaming=StreamConfig(
            mode="hooks",
            hook_inbox_path=str(inbox),
            hook_max_event_age_seconds=max_age,
        ),
        telegram=TelegramConfig(enabled=True),
    )
    store = Store(tmp_path / "store.sqlite3")
    api = FakeApi()
    return inbox, store, api, TelegramOutputRelay(config=config, store=store, api=api)


@pytest.mark.asyncio
async def test_core_output_is_delivered_to_exact_telegram_topic(tmp_path):
    inbox, store, api, output = relay(tmp_path)
    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    event_path = inbox / "event.json"
    write_v2(event_path)

    await output.drain_once(inbox)

    assert api.sent == [
        {
            "chat_id": -1001,
            "message_thread_id": 77,
            "text": "completed response",
        }
    ]
    assert not event_path.exists()
    assert store.has_event_key(
        "agent-output:telegram:-1001:77:pi:event-1"
    )


@pytest.mark.asyncio
async def test_legacy_tui_output_is_delivered_to_bound_telegram_topic(tmp_path):
    inbox, store, api, output = relay(tmp_path)
    store.upsert_telegram_topic_for_target(
        target=TargetKey.legacy("w1:p1"),
        chat_id=-1001,
        thread_id=88,
        name="project/pi",
    )
    event_path = inbox / "legacy.json"
    write_v1(event_path)

    await output.drain_once(inbox)

    assert api.sent == [
        {
            "chat_id": -1001,
            "message_thread_id": 88,
            "text": "legacy response",
        }
    ]
    assert not event_path.exists()
    assert store.has_event_key(
        "agent-output:telegram:-1001:88:pi:legacy-event-1"
    )


@pytest.mark.asyncio
async def test_core_output_fans_out_to_discord_and_telegram(tmp_path, monkeypatch):
    inbox, store, api, output = relay(tmp_path)
    route_target = TargetKey.core(
        controller_core_id="core-1", address="t12"
    ).to_route_value()
    store.upsert_agent_thread(
        pane_id=route_target,
        thread_id=123,
        guild_id=456,
        alias="project",
    )
    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    event_path = inbox / "event.json"
    write_v2(event_path)
    discord_relay = StreamManager(
        bot=None,  # type: ignore[arg-type]
        config=output.config,
        store=store,
        client=None,  # type: ignore[arg-type]
    )
    thread = AsyncMock()
    monkeypatch.setattr(
        discord_relay, "_fetch_thread", AsyncMock(return_value=thread)
    )

    await discord_relay._drain_hook_events(inbox)

    thread.send.assert_awaited_once()
    assert event_path.exists()
    assert store.has_event_key("agent-output:discord:123:pi:event-1")

    await output.drain_once(inbox)

    assert api.sent[0]["text"] == "completed response"
    assert not event_path.exists()
    assert store.has_event_key("agent-output:telegram:-1001:77:pi:event-1")


@pytest.mark.asyncio
async def test_legacy_event_is_left_for_discord_consumer(tmp_path):
    inbox, _store, api, output = relay(tmp_path)
    event_path = inbox / "legacy.json"
    event_path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "legacy-1",
                "agent": "pi",
                "pane_id": "w1:p1",
                "text": "legacy response",
            }
        )
    )

    await output.drain_once(inbox)

    assert event_path.exists()
    assert api.sent == []


@pytest.mark.asyncio
async def test_unbound_core_output_waits_then_expires(tmp_path):
    inbox, _store, api, output = relay(tmp_path, max_age=0)
    event_path = inbox / "event.json"
    write_v2(event_path)

    await output.drain_once(inbox)

    assert not event_path.exists()
    assert api.sent == []


@pytest.mark.asyncio
async def test_duplicate_core_output_is_removed_without_resend(tmp_path):
    inbox, store, api, output = relay(tmp_path)
    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    store.mark_event_seen("agent-output:telegram:-1001:77:pi:event-1")
    event_path = inbox / "event.json"
    write_v2(event_path)

    await output.drain_once(inbox)

    assert not event_path.exists()
    assert api.sent == []


@pytest.mark.asyncio
async def test_question_output_is_labeled_without_adding_controls(tmp_path):
    inbox, store, api, output = relay(tmp_path)
    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    event_path = inbox / "question.json"
    write_v2(
        event_path,
        kind="question",
        text="Choose one",
        question={"prompt": "Choose one", "options": [{"label": "One"}]},
    )

    await output.drain_once(inbox)

    assert api.sent[0]["text"] == "[pi asks]\nChoose one"
