from __future__ import annotations

import argparse
import asyncio
import logging

from .app import HerdRelayApplication
from .config import AppConfig, load_config


def run_configured(config: AppConfig) -> None:
    if not config.discord.enabled and not config.telegram.enabled:
        raise ValueError("at least one chat transport must be enabled")
    asyncio.run(HerdRelayApplication(config).run())


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
    try:
        run_configured(config)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

