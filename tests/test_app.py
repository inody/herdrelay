from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from herdrelay.app import HerdRelayApplication
from herdrelay.config import AppConfig, DiscordTransportConfig, TelegramConfig


def both_transports_config() -> AppConfig:
    return AppConfig(
        discord_token="discord-token",
        database_path="relay.sqlite3",
        telegram=TelegramConfig(enabled=True, token="telegram-token"),
    )


def test_application_builds_enabled_transports_with_shared_dependencies(monkeypatch):
    config = both_transports_config()
    seen = {}

    class FakeStore:
        def __init__(self, path):
            seen["database_path"] = path

    class FakeTelegram:
        def __init__(self, *, config, store, client):
            seen["telegram"] = (config, store, client)

    store_holder = {}

    def make_store(path):
        store = FakeStore(path)
        store_holder["store"] = store
        return store

    client = object()
    bot = object()
    monkeypatch.setattr("herdrelay.app.Store", make_store)
    monkeypatch.setattr(
        "herdrelay.app.build_backend_provider",
        lambda supplied, **kwargs: SimpleNamespace(client=client, services=()),
    )
    monkeypatch.setattr(
        "herdrelay.app.build_bot",
        lambda supplied, store, supplied_client: bot,
    )
    monkeypatch.setattr("herdrelay.app.TelegramService", FakeTelegram)

    application = HerdRelayApplication(config)

    assert seen["database_path"] == "relay.sqlite3"
    assert application.discord is bot
    assert seen["telegram"] == (config, store_holder["store"], client)


@pytest.mark.asyncio
async def test_application_runs_discord_and_telegram_as_peers(monkeypatch):
    config = both_transports_config()
    discord_started = asyncio.Event()
    telegram_started = asyncio.Event()
    release_discord = asyncio.Event()
    telegram_stopped = asyncio.Event()
    seen = {"telegram_stop": 0, "telegram_close": 0, "discord_close": 0}

    class FakeBot:
        closed = False

        async def start(self, token):
            assert token == "discord-token"
            discord_started.set()
            await release_discord.wait()

        def is_closed(self):
            return self.closed

        async def close(self):
            seen["discord_close"] += 1
            self.closed = True
            release_discord.set()

    class FakeTelegram:
        async def run_forever(self):
            telegram_started.set()
            await telegram_stopped.wait()

        def stop(self):
            seen["telegram_stop"] += 1
            telegram_stopped.set()

        async def close(self):
            seen["telegram_close"] += 1
            self.stop()

    bot = FakeBot()
    telegram = FakeTelegram()
    monkeypatch.setattr("herdrelay.app.Store", lambda path: object())
    monkeypatch.setattr(
        "herdrelay.app.build_backend_provider",
        lambda supplied, **kwargs: SimpleNamespace(client=object(), services=()),
    )
    monkeypatch.setattr(
        "herdrelay.app.build_bot",
        lambda supplied, store, client: bot,
    )
    monkeypatch.setattr(
        "herdrelay.app.TelegramService",
        lambda **kwargs: telegram,
    )

    application = HerdRelayApplication(config)
    task = asyncio.create_task(application.run())
    await asyncio.wait_for(discord_started.wait(), timeout=1)
    await asyncio.wait_for(telegram_started.wait(), timeout=1)

    release_discord.set()
    await asyncio.wait_for(task, timeout=1)

    assert seen["discord_close"] == 1
    assert seen["telegram_stop"] >= 1
    assert seen["telegram_close"] == 1


@pytest.mark.asyncio
async def test_application_stops_discord_when_telegram_fails(monkeypatch):
    config = both_transports_config()
    discord_started = asyncio.Event()
    discord_closed = asyncio.Event()

    class FakeBot:
        closed = False

        async def start(self, token):
            discord_started.set()
            await discord_closed.wait()

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True
            discord_closed.set()

    class FailingTelegram:
        async def run_forever(self):
            await discord_started.wait()
            raise RuntimeError("telegram stopped")

        def stop(self):
            pass

        async def close(self):
            pass

    bot = FakeBot()
    monkeypatch.setattr("herdrelay.app.Store", lambda path: object())
    monkeypatch.setattr(
        "herdrelay.app.build_backend_provider",
        lambda supplied, **kwargs: SimpleNamespace(client=object(), services=()),
    )
    monkeypatch.setattr(
        "herdrelay.app.build_bot",
        lambda supplied, store, client: bot,
    )
    monkeypatch.setattr(
        "herdrelay.app.TelegramService",
        lambda **kwargs: FailingTelegram(),
    )

    application = HerdRelayApplication(config)

    with pytest.raises(RuntimeError, match="telegram stopped"):
        await application.run()

    assert bot.closed is True


@pytest.mark.asyncio
async def test_application_runs_and_stops_backend_services(monkeypatch):
    config = AppConfig(discord_token="discord-token")
    backend_started = asyncio.Event()
    bot_closed = asyncio.Event()
    seen = {"backend_stop": 0}

    class FakeBot:
        closed = False

        async def start(self, token):
            await bot_closed.wait()

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True
            bot_closed.set()

    class FakeBackendService:
        async def run_forever(self):
            backend_started.set()

        def stop(self):
            seen["backend_stop"] += 1

    bot = FakeBot()
    backend_service = FakeBackendService()
    monkeypatch.setattr("herdrelay.app.Store", lambda path: object())
    monkeypatch.setattr(
        "herdrelay.app.build_backend_provider",
        lambda supplied, **kwargs: SimpleNamespace(
            client=object(), services=(backend_service,)
        ),
    )
    monkeypatch.setattr(
        "herdrelay.app.build_bot",
        lambda supplied, store, client: bot,
    )

    await HerdRelayApplication(config).run()

    assert backend_started.is_set()
    assert bot.closed is True
    assert seen["backend_stop"] == 1


def test_application_rejects_no_enabled_transport():
    config = AppConfig(
        discord_token="",
        discord=DiscordTransportConfig(enabled=False),
        telegram=TelegramConfig(enabled=False),
    )

    with pytest.raises(ValueError, match="at least one chat transport"):
        HerdRelayApplication(config)
