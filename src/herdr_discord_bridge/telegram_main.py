from __future__ import annotations

import argparse
import asyncio
import logging

from .config import AppConfig, load_config
from .runtime_client import build_runtime_client
from .store import Store
from .telegram_service import TelegramService


def _load_telegram_config(path: str) -> AppConfig:
    config = load_config(path, require_discord_token=False)
    if not config.telegram.enabled:
        raise ValueError("transports.telegram.enabled must be true")
    return config


async def run_telegram(config: AppConfig) -> None:
    store = Store(config.database_path)
    client = build_runtime_client(config)
    service = TelegramService(config=config, store=store, client=client)
    await service.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the HerdRelay Telegram transport without Discord."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = _load_telegram_config(args.config)
        asyncio.run(run_telegram(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
