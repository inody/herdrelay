from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from time import monotonic

import discord

from .config import AppConfig
from .domain import BackendKind, TargetKey
from .formatter import target_alias, target_route_name, target_route_name_for
from .models import HerdrTarget
from .runtime import RuntimeClient
from .store import Store

LOG = logging.getLogger(__name__)

THREAD_NAME_MAX = 100


def thread_name(
    target: HerdrTarget,
    *,
    key: TargetKey | None = None,
    backend: BackendKind | None = None,
) -> str:
    """Build the stable transport-neutral agent conversation name."""
    if key is not None:
        route_name = target_route_name(key, target)
    elif backend is not None:
        route_name = target_route_name_for(backend, target.target, target)
    else:
        agent = target.agent_name or target.kind or "?"
        route_name = f"{target_alias(target)}/{agent}"
    return route_name[:THREAD_NAME_MAX]


class ThreadManager:
    """Keep one Discord thread per Herdr pane, renamed to match live status."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
    ):
        self.bot = bot
        self.config = config
        self.store = store
        self.client = client
        self._stopped = asyncio.Event()
        self._membership_synced: set[int] = set()
        self._missing_since: dict[str, float] = {}

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
        # Agent exit or temporary detection loss does not close the pane.
        # A failed pane snapshot propagates before any Discord mutations.
        targets = await asyncio.to_thread(self.client.list_panes)
        active_pane_ids = {
            self.client.target_key(t.target).to_route_value()
            for t in targets
            if t.target
        }
        for target in targets:
            if not target.target:
                continue
            try:
                await self._sync_target(parent, target)
            except Exception:
                LOG.exception("Failed to sync thread for %s", target.target)
        await self._archive_stale_threads(parent, active_pane_ids)
        await self._delete_expired_threads(parent)
        self.store.clear_pending_questions_except(active_pane_ids)

    async def _sync_target(self, parent: discord.TextChannel, target: HerdrTarget) -> None:
        # Keep existing names, bindings, and archive states while the pane is a
        # shell. Only a detected agent should create or reactivate a thread.
        if not target.agent_name:
            return
        pane_id = target.target
        key = self.client.target_key(pane_id)
        route_target = key.to_route_value()
        name = thread_name(target, key=key)
        alias = target_alias(target)
        existing = self.store.get_agent_thread(route_target)
        if existing is None and route_target != pane_id:
            if self.store.migrate_agent_thread_target(
                old_target=pane_id, new_target=route_target
            ):
                existing = self.store.get_agent_thread(route_target)
                LOG.info("Pinned existing thread route %s to Core identity", pane_id)
        if existing is None:
            thread = await parent.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
            )
            self.store.upsert_agent_thread(
                pane_id=route_target,
                thread_id=thread.id,
                guild_id=parent.guild.id,
                alias=alias,
            )
            self.store.upsert_binding(
                guild_id=parent.guild.id,
                channel_id=parent.id,
                thread_id=str(thread.id),
                herdr_target=route_target,
                label=alias,
                created_by=str(self.bot.user.id) if self.bot.user else 0,
            )
            await self._ensure_allowed_members(thread, parent.guild)
            LOG.info("Created thread %s for %s", thread.id, route_target)
            return

        thread = await self._fetch_thread(parent, int(existing))
        if thread is None:
            LOG.warning(
                "Thread %s for %s not found; leaving record in place",
                existing,
                route_target,
            )
            return
        self._missing_since.pop(route_target, None)
        edits: dict[str, object] = {}
        if thread.archived:
            edits["archived"] = False
            edits["reason"] = f"Target {pane_id} active again"
        if thread.name != name:
            edits["name"] = name
        if edits:
            try:
                await thread.edit(**edits)
                self.store.upsert_agent_thread(
                    pane_id=route_target,
                    thread_id=thread.id,
                    guild_id=parent.guild.id,
                    alias=alias,
                )
                LOG.info("Updated thread %s for %s", thread.id, route_target)
            except discord.HTTPException:
                LOG.warning("Could not update thread %s (rate limited?)", thread.id)
        else:
            self.store.set_agent_thread_closed(
                pane_id=route_target, closed=False
            )
        await self._ensure_allowed_members(thread, parent.guild)

    async def _ensure_allowed_members(
        self, thread: discord.Thread, guild: discord.Guild
    ) -> None:
        if thread.id in self._membership_synced or not self.config.allowed_user_ids:
            return
        succeeded = True
        for user_id in self.config.allowed_user_ids:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.HTTPException:
                    LOG.warning(
                        "Could not resolve allowed Discord user %s for thread %s",
                        user_id,
                        thread.id,
                    )
                    succeeded = False
                    continue
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                LOG.warning(
                    "Could not add allowed Discord user %s to thread %s",
                    user_id,
                    thread.id,
                )
                succeeded = False
        if succeeded:
            self._membership_synced.add(thread.id)

    async def _archive_stale_threads(
        self, parent: discord.TextChannel, active_pane_ids: set[str]
    ) -> None:
        """Archive missing current-backend threads after grace, and old backends now."""
        now = monotonic()
        current_backend = _current_backend(self.config)
        for route in self.store.list_agent_thread_routes():
            pane_id = route.pane_id
            if pane_id in active_pane_ids:
                self._missing_since.pop(pane_id, None)
                continue
            try:
                key = TargetKey.from_route_value(pane_id)
            except ValueError:
                key = None
            switched_backend = key is not None and key.backend is not current_backend
            missing_since = self._missing_since.setdefault(pane_id, now)
            if (
                not switched_backend
                and now - missing_since
                < self.config.discord_target_absence_grace_seconds
            ):
                continue
            try:
                thread = await self._fetch_thread(parent, int(route.thread_id))
            except ValueError:
                continue
            if thread is None:
                continue
            if thread.archived:
                if not route.closed:
                    self.store.set_agent_thread_closed(pane_id=pane_id, closed=True)
                continue
            try:
                await thread.edit(archived=True, reason=f"Target {pane_id} closed")
                self.store.set_agent_thread_closed(pane_id=pane_id, closed=True)
                LOG.info("Archived thread %s (target %s gone)", route.thread_id, pane_id)
            except discord.HTTPException:
                LOG.warning("Could not archive thread %s", route.thread_id)

    async def _delete_expired_threads(self, parent: discord.TextChannel) -> None:
        retention = self.config.closed_thread_retention_minutes
        if retention is None:
            return
        cutoff = datetime.now(UTC).timestamp() - retention * 60
        for route in self.store.list_agent_thread_routes():
            if not route.closed or _timestamp(route.updated_at) > cutoff:
                continue
            thread = None
            try:
                thread = await self._fetch_thread(parent, int(route.thread_id))
            except ValueError:
                pass
            if thread is not None:
                try:
                    await thread.delete(reason="HerdRelay closed-thread retention expired")
                except discord.HTTPException:
                    LOG.warning("Could not delete expired thread %s", route.thread_id)
                    continue
            self.store.delete_binding(
                guild_id=route.guild_id,
                channel_id=parent.id,
                thread_id=route.thread_id,
            )
            self.store.delete_agent_thread(pane_id=route.pane_id)
            self._membership_synced.discard(int(route.thread_id))
            self._missing_since.pop(route.pane_id, None)
            LOG.info("Deleted expired thread %s", route.thread_id)

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


def _current_backend(config: AppConfig) -> BackendKind:
    return {
        "herdr-tui": BackendKind.HERDR_TUI,
        "herdr-core": BackendKind.HERDR_CORE,
        "orca": BackendKind.ORCA,
    }[config.runtime.provider]


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0
