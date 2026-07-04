from __future__ import annotations

import asyncio
import logging

import discord

from .config import AppConfig
from .formatter import split_tail_chunks, wrap_code_block
from .herdr_client import HerdrClient
from .models import HerdrTarget
from .store import Store

LOG = logging.getLogger(__name__)


def compute_stream_diff(prev_tail: str | None, current_tail: str) -> str:
    """Return the new lines in current_tail that came after the last line of prev_tail."""
    if not prev_tail:
        return current_tail
    prev_lines = prev_tail.split("\n")
    curr_lines = current_tail.split("\n")
    marker = _last_meaningful_line(prev_lines)
    if marker is None:
        return current_tail
    for i in range(len(curr_lines) - 1, -1, -1):
        if curr_lines[i] == marker:
            return "\n".join(curr_lines[i + 1 :])
    return current_tail


def _last_meaningful_line(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if line.strip():
            return line
    return None


class StreamManager:
    """Stream new pane output into the bound Discord thread for each pane."""

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
                LOG.exception("Stream sync failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.streaming.refresh_seconds,
                )
            except TimeoutError:
                pass

    async def sync(self) -> None:
        targets = self.client.list_targets()
        for target in targets:
            if not target.target:
                continue
            try:
                await self._stream_target(target)
            except Exception:
                LOG.exception("Failed to stream %s", target.target)

    async def _stream_target(self, target: HerdrTarget) -> None:
        pane_id = target.target
        thread_id = self.store.get_agent_thread(pane_id)
        if thread_id is None:
            return
        thread = await self._fetch_thread(int(thread_id))
        if thread is None:
            return
        try:
            current = self.client.read(
                pane_id, lines=self.config.streaming.tail_lines, fmt="text", source="visible"
            )
        except Exception:
            LOG.exception("Failed to read tail for streaming %s", pane_id)
            return
        prev = self.store.get_pane_stream(pane_id)
        diff = compute_stream_diff(prev, current)
        self.store.upsert_pane_stream(pane_id, current)
        if not diff or not diff.strip():
            return
        for chunk in split_tail_chunks(diff, max_chars=self.config.max_output_chars):
            try:
                await thread.send(wrap_code_block(chunk))
            except discord.HTTPException:
                LOG.warning("Failed to post stream chunk to thread %s", thread_id)

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except Exception:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None
