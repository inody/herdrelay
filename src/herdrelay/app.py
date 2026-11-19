from __future__ import annotations

import asyncio
import logging

from .backends import BackendProvider, build_backend_provider
from .config import AppConfig
from .discord_bot import HerdrDiscordBot, build_bot
from .runtime import RuntimeClient
from .store import Store
from .telegram_service import TelegramService

LOG = logging.getLogger(__name__)


class HerdRelayApplication:
    """Own the selected runtime and all enabled chat transports as peers."""

    def __init__(self, config: AppConfig):
        if not config.discord.enabled and not config.telegram.enabled:
            raise ValueError("at least one chat transport must be enabled")

        self.config = config
        self.store = Store(config.database_path)
        self.backend_provider: BackendProvider = build_backend_provider(
            config, store=self.store
        )
        self.client: RuntimeClient = self.backend_provider.client
        self.discord: HerdrDiscordBot | None = (
            build_bot(config, self.store, self.client)
            if config.discord.enabled
            else None
        )
        self.telegram: TelegramService | None = (
            TelegramService(config=config, store=self.store, client=self.client)
            if config.telegram.enabled
            else None
        )
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    async def run(self) -> None:
        if self._tasks:
            raise RuntimeError("HerdRelay application is already running")

        if self.discord is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.discord.start(self.config.discord_token),
                    name="herdrelay-discord",
                )
            )
        if self.telegram is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.telegram.run_forever(),
                    name="herdrelay-telegram",
                )
            )
        for index, service in enumerate(self.backend_provider.services):
            self._tasks.append(
                asyncio.create_task(
                    service.run_forever(),
                    name=f"herdrelay-backend-{index}",
                )
            )

        try:
            done, _ = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self.telegram is not None:
            self.telegram.stop()
        for service in self.backend_provider.services:
            service.stop()
        if self.discord is not None and not self.discord.is_closed():
            await self.discord.close()

        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.telegram is not None:
            await self.telegram.close()

        LOG.info("HerdRelay application stopped")
