from __future__ import annotations

import asyncio
import logging

import discord

from .config import AppConfig
from .formatter import target_alias
from .herdr_client import HerdrClient
from .models import HerdrTarget
from .store import Store

LOG = logging.getLogger(__name__)

THREAD_NAME_MAX = 100


def thread_name(target: HerdrTarget) -> str:
    """Build a Discord thread name, prefixed with a marker only when blocked.

    Status changes between working/idle/done are frequent and would trip Discord's
    channel-edit rate limits, so we only mark blocked panes distinctly.
    """
    alias = target_alias(target)
    agent = target.agent_name or target.kind or "?"
    status = (target.status or "").casefold()
    prefix = "🔴 " if status == "blocked" else ""
    return f"{prefix}{alias}/{agent}"[:THREAD_NAME_MAX]


class ThreadManager:
    """Keep one Discord thread per Herdr pane, renamed to match live status."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        config: AppConfig,
        store: Store,
        client: HerdrClient,
    ):
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
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Thread sync failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.auto_threads.refresh_seconds,
                )
            except TimeoutError:
                pass

    async def sync(self) -> None:
        parent = await self._parent_channel()
        targets = self.client.list_targets()
        active_pane_ids = {t.target for t in targets if t.target}
        for target in targets:
            if not target.target:
                continue
            try:
                await self._sync_target(parent, target)
            except Exception:
                LOG.exception("Failed to sync thread for %s", target.target)
        await self._archive_stale_threads(parent, active_pane_ids)

    async def _sync_target(self, parent: discord.TextChannel, target: HerdrTarget) -> None:
        pane_id = target.target
        name = thread_name(target)
        alias = target_alias(target)
        existing = self.store.get_agent_thread(pane_id)
        if existing is None:
            thread = await parent.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
            )
            self.store.upsert_agent_thread(
                pane_id=pane_id,
                thread_id=thread.id,
                guild_id=parent.guild.id,
                alias=alias,
            )
            self.store.upsert_binding(
                guild_id=parent.guild.id,
                channel_id=parent.id,
                thread_id=str(thread.id),
                herdr_target=pane_id,
                label=alias,
                created_by=str(self.bot.user.id) if self.bot.user else 0,
            )
            LOG.info("Created thread %s for %s", thread.id, pane_id)
            return

        thread = await self._fetch_thread(parent, int(existing))
        if thread is None:
            LOG.warning(
                "Thread %s for %s not found; leaving record in place", existing, pane_id
            )
            return
        if thread.archived:
            try:
                await thread.edit(archived=False, reason=f"Pane {pane_id} active again")
                LOG.info("Unarchived thread %s for %s", thread.id, pane_id)
            except discord.HTTPException:
                LOG.warning("Could not unarchive thread %s", thread.id)
        if thread.name == name:
            return
        try:
            await thread.edit(name=name)
            self.store.upsert_agent_thread(
                pane_id=pane_id,
                thread_id=thread.id,
                guild_id=parent.guild.id,
                alias=alias,
            )
            LOG.info("Renamed thread %s to %s", thread.id, name)
        except discord.HTTPException:
            LOG.warning("Could not rename thread %s (rate limited?)", thread.id)

    async def _archive_stale_threads(
        self, parent: discord.TextChannel, active_pane_ids: set[str]
    ) -> None:
        """Archive threads whose pane no longer exists."""
        for pane_id, thread_id_str in self.store.list_agent_threads().items():
            if pane_id in active_pane_ids:
                continue
            try:
                thread = await self._fetch_thread(parent, int(thread_id_str))
            except ValueError:
                continue
            if thread is None or thread.archived:
                continue
            try:
                await thread.edit(archived=True, reason=f"Pane {pane_id} closed")
                LOG.info("Archived thread %s (pane %s gone)", thread_id_str, pane_id)
            except discord.HTTPException:
                LOG.warning("Could not archive thread %s", thread_id_str)

    async def _fetch_thread(
        self, parent: discord.TextChannel, thread_id: int
    ) -> discord.Thread | None:
        thread = parent.guild.get_thread(thread_id)
        if thread is not None:
            return thread
        try:
            channel = await self.bot.fetch_channel(thread_id)
        except Exception:
            return None
        return channel if isinstance(channel, discord.Thread) else None

    async def _parent_channel(self) -> discord.TextChannel:
        channel_id = self.config.thread_parent_channel_id
        if channel_id is None:
            raise RuntimeError("thread_parent_channel_id is required for auto threads")
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Thread parent channel is not a text channel: {channel_id}")
        return channel
