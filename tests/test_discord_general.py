from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from herdrelay.config import (
    AppConfig,
    RuntimeConfig,
    TelegramConfig,
    TelegramGeneralRouterConfig,
    TelegramOrcaBotConfig,
)
from herdrelay.discord_bot import HerdrDiscordBot
from herdrelay.discord_general import DiscordGeneralRouter, DiscordOrcaBotGeneral
from herdrelay.domain import TargetKey
from herdrelay.models import HerdrTarget
from herdrelay.orca_bot_gateway import GatewayQueue
from herdrelay.store import Store
from herdrelay.telegram_router import RoutingDecision


class FakeModel:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def classify(self, request):
        self.requests.append(request)
        return self.decision


class FakeClient:
    def __init__(self, target, key):
        self.target = target
        self.key = key
        self.sent = []

    def list_targets(self):
        return [self.target]

    def target_key(self, address):
        assert address == self.key.address
        return self.key

    def resolve_target_key(self, key):
        assert key == self.key
        return self.target

    def send(self, address, text):
        self.sent.append((address, text))


def message(content="チャットブリッジのパイにテストして", *, message_id=10):
    channel = SimpleNamespace(id=100, send=AsyncMock())
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=42),
        guild=SimpleNamespace(id=1),
        channel=channel,
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )


def router_config(tmp_path):
    return AppConfig(
        discord_token="token",
        database_path=str(tmp_path / "store.sqlite3"),
        thread_parent_channel_id=100,
        enable_send=True,
        runtime=RuntimeConfig(provider="herdr-tui"),
        telegram=TelegramConfig(
            general_router=TelegramGeneralRouterConfig(
                enabled=True,
                provider="router",
                context_ttl_seconds=1800,
            )
        ),
    )


@pytest.mark.asyncio
async def test_discord_general_router_sends_to_exact_bound_candidate(tmp_path):
    config = router_config(tmp_path)
    store = Store(config.database_path)
    key = TargetKey.legacy("w1:p1")
    target = HerdrTarget(
        target=key.address,
        workspace_label="herdr-chat-bridge",
        agent_name="pi",
        status="idle",
    )
    store.upsert_binding(
        guild_id=1,
        channel_id=100,
        thread_id=200,
        herdr_target=key.to_route_value(),
        label="herdr-chat-bridge",
        created_by=1,
    )
    model = FakeModel(
        RoutingDecision(
            intent="send_prompt",
            candidate_id="candidate-1",
            message_verbatim="テストして",
            ambiguous=False,
            reason=None,
            routing_basis="explicit_destination",
        )
    )
    client = FakeClient(target, key)
    handler = DiscordGeneralRouter(
        config=config, store=store, client=client, model=model
    )
    inbound = message()

    assert await handler.handle(inbound) is True

    assert client.sent == [("w1:p1", "テストして")]
    inbound.add_reaction.assert_awaited_once_with("✅")
    assert model.requests[0].public_data()["candidates"] == [
        {
            "id": "candidate-1",
            "workspace": "herdr-chat-bridge",
            "title": None,
            "agent": "pi",
            "status": "idle",
            "alias": "herdr-chat-bridge",
        }
    ]
    assert store.get_state("discord:general-context:1:100:42") is not None


@pytest.mark.asyncio
async def test_discord_general_router_fails_closed_on_ambiguous_decision(tmp_path):
    config = router_config(tmp_path)
    store = Store(config.database_path)
    key = TargetKey.legacy("w1:p1")
    target = HerdrTarget(target=key.address, workspace_label="project", agent_name="pi")
    store.upsert_binding(
        guild_id=1,
        channel_id=100,
        thread_id=200,
        herdr_target=key.to_route_value(),
        label="project",
        created_by=1,
    )
    model = FakeModel(RoutingDecision("unknown", None, None, True, "ambiguous"))
    client = FakeClient(target, key)
    handler = DiscordGeneralRouter(
        config=config, store=store, client=client, model=model
    )
    inbound = message()

    assert await handler.handle(inbound) is False

    assert client.sent == []
    assert inbound.reply.await_args.args[0].startswith(
        "A single destination could not be selected."
    )


@pytest.mark.asyncio
async def test_discord_parent_channel_is_consumed_by_general_handler(tmp_path):
    config = AppConfig(
        discord_token="token",
        database_path=str(tmp_path / "bot.sqlite3"),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({100}),
        allowed_user_ids=frozenset({42}),
        thread_parent_channel_id=100,
        enable_send=True,
        telegram=TelegramConfig(
            general_router=TelegramGeneralRouterConfig(
                enabled=True, provider="router"
            )
        ),
    )
    model = FakeModel(RoutingDecision("unknown", None, None, True, "ambiguous"))
    bot = HerdrDiscordBot(
        config=config,
        store=Store(config.database_path),
        client=SimpleNamespace(),
        router_model=model,
    )
    handler = SimpleNamespace(handle=AsyncMock(return_value=False))
    bot.general_handler = handler
    inbound = message()
    inbound.author.bot = False

    await bot.on_message(inbound)

    handler.handle.assert_awaited_once_with(inbound)
    await bot.close()


@pytest.mark.asyncio
async def test_discord_orca_bot_uses_transport_specific_queue_and_reply(tmp_path):
    queue = GatewayQueue(tmp_path / "queue")
    config = AppConfig(
        discord_token="token",
        enable_send=True,
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            general_router=TelegramGeneralRouterConfig(
                enabled=True,
                provider="orca-bot",
                orca_bot=TelegramOrcaBotConfig(
                    bot_id="bot-1",
                    queue_path=str(queue.root),
                    gateway_worktree="folder:bot",
                    gateway_command="run gateway",
                ),
            )
        ),
    )
    store = Store(tmp_path / "store.sqlite3")
    handler = DiscordOrcaBotGeneral(config=config, store=store, queue=queue)
    inbound = message("Botに聞いて", message_id=99)

    assert await handler.handle(inbound) is True

    request = json.loads((queue.requests / "discord-99.json").read_text())
    assert request["transport"] == "discord"
    assert request["channel_id"] == 100
    inbound.add_reaction.assert_awaited_once_with("👀")

    client = SimpleNamespace(
        get_channel=lambda channel_id: inbound.channel,
        fetch_channel=AsyncMock(),
    )
    handler.client = client
    await handler.handle_gateway_event(
        {
            "kind": "reply",
            "request_id": "discord-99",
            "channel_id": 100,
            "guild_id": 1,
            "user_id": 42,
            "text": "Bot answer",
        }
    )
    inbound.channel.send.assert_awaited_once()
    assert inbound.channel.send.await_args.args[0] == "Bot answer"
