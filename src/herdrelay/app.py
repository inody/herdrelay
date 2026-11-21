from __future__ import annotations

import asyncio
import logging

from .backends import BackendProvider, build_backend_provider
from .config import AppConfig, effective_general_provider
from .discord_bot import HerdrDiscordBot, build_bot
from .orca_bot_gateway import GatewayQueue
from .runtime import RuntimeClient
from .store import Store
from .telegram_orca_bot import OrcaBotGatewaySupervisor
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
        self._gateway_stopped = asyncio.Event()
        self.gateway_queue: GatewayQueue | None = None
        if (
            config.telegram.general_router.enabled
            and effective_general_provider(config) == "orca-bot"
        ):
            self.gateway_queue = GatewayQueue(
                config.telegram.general_router.orca_bot.queue_path
            )
        if config.discord.enabled:
            if self.gateway_queue is None:
                self.discord = build_bot(config, self.store, self.client)
            else:
                self.discord = build_bot(
                    config,
                    self.store,
                    self.client,
                    gateway_queue=self.gateway_queue,
                )
        else:
            self.discord = None
        if config.telegram.enabled:
            if self.gateway_queue is None:
                self.telegram = TelegramService(
                    config=config, store=self.store, client=self.client
                )
            else:
                self.telegram = TelegramService(
                    config=config,
                    store=self.store,
                    client=self.client,
                    gateway_queue=self.gateway_queue,
                    supervise_gateway=False,
                )
        else:
            self.telegram = None
        self.gateway: OrcaBotGatewaySupervisor | None = (
            OrcaBotGatewaySupervisor(
                config=config,
                store=self.store,
                stopped=self._gateway_stopped,
                queue=self.gateway_queue,
                deliver_event=self._deliver_gateway_event,
            )
            if self.gateway_queue is not None
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
        if self.gateway is not None:
            self._tasks.append(
                asyncio.create_task(
                    self.gateway.run_forever(), name="herdrelay-orca-bot-gateway"
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

    async def _deliver_gateway_event(self, event: dict) -> None:
        transport = event.get("transport") or "telegram"
        if transport == "telegram" and self.telegram is not None:
            handler = self.telegram.general_router
            deliver = getattr(handler, "handle_gateway_event", None)
            if deliver is not None:
                await deliver(event)
                return
        if transport == "discord" and self.discord is not None:
            handler = self.discord.general_handler
            deliver = getattr(handler, "handle_gateway_event", None)
            if deliver is not None:
                await deliver(event)
                return
        raise RuntimeError(f"No General event destination for transport {transport!r}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gateway_stopped.set()

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
