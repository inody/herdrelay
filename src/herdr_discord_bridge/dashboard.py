from __future__ import annotations

import asyncio
import logging

import discord

from .config import AppConfig
from .formatter import format_dashboard
from .herdr_client import HerdrClient
from .store import Store

LOG = logging.getLogger(__name__)

DASHBOARD_MESSAGE_ID_KEY = "dashboard_message_id"


class DashboardManager:
    def __init__(self, *, bot: discord.Client, config: AppConfig, store: Store, client: HerdrClient):
        self.bot = bot
        self.config = config
        self.store = store
        self.client = client
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Dashboard refresh failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.dashboard.refresh_seconds,
                )
            except TimeoutError:
                pass

    async def refresh(self, *, recreate: bool = False) -> discord.Message:
        channel = await self._dashboard_channel()
        targets = self.client.list_targets()
        content = format_dashboard(targets, max_chars=self.config.max_output_chars)
        message = None if recreate else await self._existing_message(channel)
        if message:
            await message.edit(content=content)
            LOG.info("Updated dashboard message %s", message.id)
            return message
        message = await channel.send(content)
        self.store.set_state(DASHBOARD_MESSAGE_ID_KEY, str(message.id))
        LOG.info("Created dashboard message %s", message.id)
        return message

    async def _dashboard_channel(self) -> discord.abc.Messageable:
        channel_id = self.config.dashboard_channel_id
        if channel_id is None:
            raise RuntimeError("dashboard_channel_id is required for dashboard")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Dashboard channel is not messageable: {channel_id}")
        return channel

    async def _existing_message(self, channel: discord.abc.Messageable) -> discord.Message | None:
        message_id = self.store.get_state(DASHBOARD_MESSAGE_ID_KEY)
        if not message_id or not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(int(message_id))
        except discord.NotFound:
            return None
        except discord.Forbidden:
            LOG.exception("Cannot fetch dashboard message %s", message_id)
            return None
