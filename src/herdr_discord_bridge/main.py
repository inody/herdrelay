from __future__ import annotations

import argparse
import logging

from .config import load_config
from .discord_bot import build_bot
from .herdr_client import HerdrClient
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Herdr Discord bridge bot.")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    store = Store(config.database_path)
    client = HerdrClient(config)
    bot = build_bot(config, store, client)
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()

