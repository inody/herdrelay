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
        self._prev_recent: dict[str, str] = {}
        self._prev_visible: dict[str, str] = {}

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
        targets = await asyncio.to_thread(self.client.list_targets)
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
        tail_lines = self.config.streaming.tail_lines
        try:
            current_recent = await asyncio.to_thread(
                self.client.read,
                pane_id,
                lines=tail_lines,
                fmt="text",
                source="recent",
            )
        except Exception:
            LOG.exception("Failed to read recent tail for streaming %s", pane_id)
            return
        diff = compute_stream_diff(self._prev_recent.get(pane_id), current_recent)
        self._prev_recent[pane_id] = current_recent
        if diff and diff.strip():
            await self._send_diff(thread, thread_id, diff)
            return
        # No change in agent output — check the visible screen for pager/TUI
        # updates (e.g. Claude's /usage) that recent does not capture.
        try:
            current_visible = await asyncio.to_thread(
                self.client.read,
                pane_id,
                lines=tail_lines,
                fmt="text",
                source="visible",
            )
        except Exception:
            LOG.exception("Failed to read visible tail for streaming %s", pane_id)
            return
        diff = compute_stream_diff(self._prev_visible.get(pane_id), current_visible)
        self._prev_visible[pane_id] = current_visible
        if diff and diff.strip():
            await self._send_diff(thread, thread_id, diff)

    async def _send_diff(
        self, thread: discord.Thread, thread_id: int | str, diff: str
    ) -> None:
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
