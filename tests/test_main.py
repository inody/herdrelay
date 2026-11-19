from __future__ import annotations

import pytest

from herdrelay.config import (
    AppConfig,
    DiscordTransportConfig,
    TelegramConfig,
)
from herdrelay.main import run_configured


def test_run_configured_runs_application(monkeypatch):
    config = AppConfig(discord_token="discord-token")
    seen = {}

    class FakeApplication:
        def __init__(self, supplied):
            seen["config"] = supplied

        async def run(self):
            seen["ran"] = True

    monkeypatch.setattr("herdrelay.main.HerdRelayApplication", FakeApplication)

    run_configured(config)

    assert seen == {"config": config, "ran": True}


def test_run_configured_can_run_telegram_without_discord(monkeypatch):
    config = AppConfig(
        discord_token="",
        discord=DiscordTransportConfig(enabled=False),
        telegram=TelegramConfig(enabled=True),
    )
    seen = {}

    class FakeApplication:
        def __init__(self, supplied):
            seen["config"] = supplied

        async def run(self):
            seen["ran"] = True

    monkeypatch.setattr("herdrelay.main.HerdRelayApplication", FakeApplication)

    run_configured(config)

    assert seen == {"config": config, "ran": True}


def test_run_configured_rejects_no_enabled_transport():
    config = AppConfig(
        discord_token="",
        discord=DiscordTransportConfig(enabled=False),
        telegram=TelegramConfig(enabled=False),
    )

    with pytest.raises(ValueError, match="at least one chat transport"):
        run_configured(config)
