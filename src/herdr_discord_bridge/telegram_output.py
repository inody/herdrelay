from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time

from .config import AppConfig
from .domain import OutputEventKind, RuntimeKind
from .output_events import inbox_events_in_arrival_order, load_hook_output_event
from .store import Store
from .telegram_api import TelegramBotApi

LOG = logging.getLogger(__name__)


class TelegramOutputRelay:
    """Deliver Core-aware adapter events to their exact Telegram topic."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        api: TelegramBotApi,
    ):
        self.config = config
        self.store = store
        self.api = api
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        inbox = Path(self.config.streaming.hook_inbox_path).expanduser()
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            inbox.chmod(0o700)
        except OSError:
            LOG.warning("Could not restrict hook inbox permissions: %s", inbox)
        LOG.info("Watching Core output inbox for Telegram delivery: %s", inbox)
        while not self._stopped.is_set():
            try:
                await self.drain_once(inbox)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Telegram output inbox scan failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.streaming.hook_refresh_seconds,
                )
            except TimeoutError:
                pass

    async def drain_once(self, inbox: Path) -> None:
        paths = await asyncio.to_thread(inbox_events_in_arrival_order, inbox)
        for path in paths:
            try:
                event = await asyncio.to_thread(
                    load_hook_output_event,
                    path,
                    max_bytes=self.config.streaming.hook_max_event_bytes,
                )
            except FileNotFoundError:
                continue
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                LOG.warning("Discarding invalid agent output event %s: %s", path.name, exc)
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            if event.target.runtime is not RuntimeKind.HERDR_CORE:
                continue
            core_id = event.target.controller_core_id
            assert core_id is not None
            route = self.store.get_telegram_topic(
                core_id=core_id,
                terminal_address=event.target.address,
            )
            if route is None or route.closed:
                if await asyncio.to_thread(self._expired, path):
                    LOG.warning(
                        "Discarding expired %s output for unbound Core target %s/%s",
                        event.agent,
                        core_id,
                        event.target.address,
                    )
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            dedupe_key = (
                f"agent-output:telegram:{route.chat_id}:{route.thread_id}:"
                f"{event.agent}:{event.event_id}"
            )
            if self.store.has_event_key(dedupe_key):
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            if event.kind in {
                OutputEventKind.HUMAN_INPUT.value,
                OutputEventKind.QUESTION_RESOLVED.value,
            }:
                self.store.mark_event_seen(dedupe_key)
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            text = event.text
            if event.kind == OutputEventKind.QUESTION.value:
                text = f"[{event.agent} asks]\n{text}"
            await self.api.send_text(
                chat_id=int(route.chat_id),
                message_thread_id=int(route.thread_id),
                text=text,
            )
            self.store.mark_event_seen(dedupe_key)
            await asyncio.to_thread(path.unlink, missing_ok=True)
            LOG.info(
                "Delivered %s output to Telegram topic %s",
                event.agent,
                route.thread_id,
            )

    def _expired(self, path: Path) -> bool:
        try:
            return (
                time.time() - path.stat().st_mtime
                >= self.config.streaming.hook_max_event_age_seconds
            )
        except FileNotFoundError:
            return False
