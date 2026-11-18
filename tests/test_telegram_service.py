from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from herdr_discord_bridge.config import (
    AppConfig,
    HcRuntimeConfig,
    RuntimeConfig,
    TelegramConfig,
)
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.telegram_api import TelegramApiError
from herdr_discord_bridge.telegram_service import (
    OFFSET_STATE_KEY,
    TelegramReadOnlyService,
)


def update(*, update_id=10, user_id=42, chat_id=-1001, text="/agents"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "message_thread_id": 77,
            "from": {"id": user_id, "is_bot": False},
            "chat": {
                "id": chat_id,
                "type": "supergroup",
                "is_forum": True,
            },
            "text": text,
        },
    }


def config() -> AppConfig:
    return AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(
            provider="herdr-core",
            core=HcRuntimeConfig(allowed_terminal_addresses=frozenset({"t1"})),
        ),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
        ),
    )


@dataclass
class FakeApi:
    batches: list[list[dict]]
    fail_send: bool = False
    update_calls: list[dict] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    closed: bool = False

    async def get_updates(self, **kwargs):
        self.update_calls.append(kwargs)
        return self.batches.pop(0) if self.batches else []

    async def send_text(self, **kwargs):
        if self.fail_send:
            raise TelegramApiError("send failed")
        self.sent.append(kwargs)
        return ()

    async def close(self):
        self.closed = True


@dataclass
class FakeClient:
    calls: int = 0

    def list_targets(self):
        self.calls += 1
        return [
            HerdrTarget(
                target="t1",
                agent_name="pi",
                status="working",
                workspace_label="project",
            )
        ]


def service(tmp_path, api: FakeApi, client: FakeClient | None = None):
    client = client or FakeClient()
    result = TelegramReadOnlyService(
        config=config(),
        store=Store(tmp_path / "telegram.sqlite3"),
        client=client,
        api=api,
    )
    return result, client


@pytest.mark.asyncio
async def test_authorized_agents_command_lists_targets_in_same_topic(tmp_path):
    api = FakeApi([[update()]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 1
    assert api.sent == [
        {
            "chat_id": -1001,
            "message_thread_id": 77,
            "text": "Agents\nworking  t1  project/pi",
        }
    ]
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"
    assert api.update_calls[0]["allowed_updates"] == ("message",)


@pytest.mark.asyncio
async def test_unauthorized_update_makes_no_core_or_telegram_send(tmp_path):
    api = FakeApi([[update(user_id=9)]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert api.sent == []
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"


@pytest.mark.asyncio
async def test_normal_text_is_read_only_and_does_not_call_core(tmp_path):
    api = FakeApi([[update(text="please continue")]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert len(api.sent) == 1
    assert "read-only" in api.sent[0]["text"]
    assert "no Core mutation" in api.sent[0]["text"]


@pytest.mark.asyncio
async def test_duplicate_update_is_not_delivered_twice(tmp_path):
    item = update()
    api = FakeApi([[item], [item]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()
    await relay.poll_once()

    assert client.calls == 1
    assert len(api.sent) == 1
    assert api.update_calls[1]["offset"] == 11


@pytest.mark.asyncio
async def test_delivery_failure_does_not_advance_or_dedupe_update(tmp_path):
    api = FakeApi([[update()]], fail_send=True)
    relay, client = service(tmp_path, api)

    with pytest.raises(TelegramApiError, match="send failed"):
        await relay.poll_once()

    assert client.calls == 1
    assert relay.store.get_state(OFFSET_STATE_KEY) is None
    assert relay.store.has_event_key("telegram:update:10") is False


@pytest.mark.asyncio
async def test_invalid_message_is_discarded_and_offset_advances(tmp_path):
    item = update()
    item["message"]["chat"]["id"] = "bad"
    api = FakeApi([[item]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert api.sent == []
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"
    assert relay.store.has_event_key("telegram:update:10") is True
