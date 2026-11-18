from __future__ import annotations

import pytest

from herdr_discord_bridge.config import (
    AppConfig,
    DiscordTransportConfig,
    TelegramConfig,
)
from herdr_discord_bridge.main import run_configured


def test_run_configured_prefers_enabled_discord(monkeypatch):
    config = AppConfig(discord_token="discord-token")
    seen = {}

    class FakeBot:
        def run(self, token):
            seen["token"] = token

    store = object()
    client = object()
    monkeypatch.setattr("herdr_discord_bridge.main.Store", lambda path: store)
    monkeypatch.setattr(
        "herdr_discord_bridge.main.build_runtime_client", lambda supplied: client
    )
    monkeypatch.setattr(
        "herdr_discord_bridge.main.build_bot",
        lambda supplied, supplied_store, supplied_client: FakeBot(),
    )

    run_configured(config)

    assert seen["token"] == "discord-token"


def test_run_configured_can_run_telegram_without_discord(monkeypatch):
    config = AppConfig(
        discord_token="",
        discord=DiscordTransportConfig(enabled=False),
        telegram=TelegramConfig(enabled=True),
    )
    seen = {}

    async def fake_run_telegram(supplied):
        seen["config"] = supplied

    monkeypatch.setattr("herdr_discord_bridge.main.run_telegram", fake_run_telegram)

    run_configured(config)

    assert seen["config"] is config


def test_run_configured_rejects_no_enabled_transport():
    config = AppConfig(
        discord_token="",
        discord=DiscordTransportConfig(enabled=False),
        telegram=TelegramConfig(enabled=False),
    )

    with pytest.raises(ValueError, match="at least one chat transport"):
        run_configured(config)
