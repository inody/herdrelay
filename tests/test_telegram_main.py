from __future__ import annotations

import pytest

from herdr_discord_bridge.config import (
    AppConfig,
    HcRuntimeConfig,
    RuntimeConfig,
    TelegramConfig,
)
from herdr_discord_bridge.telegram_main import _load_telegram_config, run_telegram


def test_load_telegram_config_does_not_require_discord_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "telegram.yaml"
    config_path.write_text(
        """
runtime:
  provider: herdr-core
transports:
  telegram:
    enabled: true
    allowed_chat_ids: [-1001]
    allowed_user_ids: [42]
    forum_chat_id: -1001
"""
    )

    config = _load_telegram_config(str(config_path))

    assert config.discord_token == ""
    assert config.telegram.token == "telegram-token"


def test_load_telegram_config_requires_enabled_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "")
    config_path = tmp_path / "telegram.yaml"
    config_path.write_text("runtime:\n  provider: herdr-core\n")

    with pytest.raises(ValueError, match="transports.telegram.enabled"):
        _load_telegram_config(str(config_path))


@pytest.mark.asyncio
async def test_run_telegram_wires_store_runtime_and_service(monkeypatch):
    config = AppConfig(
        discord_token="",
        database_path="telegram.sqlite3",
        runtime=RuntimeConfig(
            provider="herdr-core",
            core=HcRuntimeConfig(allowed_terminal_addresses=frozenset({"t12"})),
        ),
        telegram=TelegramConfig(
            enabled=True,
            token="telegram-token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
        ),
    )
    seen = {}

    class FakeStore:
        def __init__(self, path):
            seen["database_path"] = path

    class FakeService:
        def __init__(self, *, config, store, client):
            seen["config"] = config
            seen["store"] = store
            seen["client"] = client

        async def run_forever(self):
            seen["ran"] = True

    runtime_client = object()
    monkeypatch.setattr("herdr_discord_bridge.telegram_main.Store", FakeStore)
    monkeypatch.setattr(
        "herdr_discord_bridge.telegram_main.build_runtime_client",
        lambda supplied: runtime_client,
    )
    monkeypatch.setattr(
        "herdr_discord_bridge.telegram_main.TelegramService", FakeService
    )

    await run_telegram(config)

    assert seen["database_path"] == "telegram.sqlite3"
    assert seen["config"] is config
    assert seen["client"] is runtime_client
    assert seen["ran"] is True
