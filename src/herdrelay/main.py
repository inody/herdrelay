from __future__ import annotations

import argparse
import asyncio
import logging

from .config import AppConfig, load_config
from .discord_bot import build_bot
from .runtime_client import build_runtime_client
from .store import Store
from .telegram_main import run_telegram


def run_configured(config: AppConfig) -> None:
    if config.discord.enabled:
        store = Store(config.database_path)
        client = build_runtime_client(config)
        bot = build_bot(config, store, client)
        bot.run(config.discord_token)
        return
    if config.telegram.enabled:
        try:
            asyncio.run(run_telegram(config))
        except KeyboardInterrupt:
            pass
        return
    raise ValueError("at least one chat transport must be enabled")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run configured HerdRelay transports.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    run_configured(config)


if __name__ == "__main__":
    main()

