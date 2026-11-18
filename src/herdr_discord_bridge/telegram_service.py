from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import AppConfig
from .formatter import target_alias
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramApiError, TelegramBotApi
from .telegram_updates import TelegramUpdatePolicy, parse_inbound_message

LOG = logging.getLogger(__name__)
OFFSET_STATE_KEY = "telegram_next_update_id"


class TelegramReadOnlyService:
    """Authorized outbound long polling with read-only agent commands."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
        api: TelegramBotApi | None = None,
    ):
        self.config = config
        self.telegram = config.telegram
        self.store = store
        self.client = client
        self.api = api or TelegramBotApi(
            self.telegram.token,
            request_timeout_seconds=self.telegram.request_timeout_seconds,
        )
        self.policy = TelegramUpdatePolicy(self.telegram)
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def close(self) -> None:
        self.stop()
        await self.api.close()

    async def run_forever(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Telegram polling failed; reconnecting")
                    await self._sleep_reconnect()
        finally:
            await self.api.close()

    async def poll_once(self) -> None:
        offset = self._load_offset()
        updates = await self.api.get_updates(
            offset=offset,
            timeout_seconds=self.telegram.poll_timeout_seconds,
            allowed_updates=("message",),
        )
        for update in updates:
            try:
                update_id = _update_id(update)
            except ValueError as exc:
                LOG.warning("Discarding invalid Telegram update: %s", exc)
                continue
            dedupe_key = f"telegram:update:{update_id}"
            if self.store.has_event_key(dedupe_key):
                self._save_offset(update_id + 1)
                continue
            try:
                await self._handle_update(update)
            except ValueError as exc:
                LOG.warning("Discarding invalid Telegram update %s: %s", update_id, exc)
            if not self.store.mark_event_seen(dedupe_key):
                LOG.info("Telegram update %s was already handled", update_id)
            self._save_offset(update_id + 1)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = parse_inbound_message(update)
        if message is None or not self.policy.authorized(message):
            return
        command = message.command
        if command in {"agents", "list"}:
            targets = await asyncio.to_thread(self.client.list_targets)
            if not targets:
                text = "No allowlisted agents are currently available."
            else:
                lines = []
                for target in targets:
                    status = target.status or "unknown"
                    agent = target.agent_name or target.kind or "?"
                    lines.append(
                        f"{status}  {target.target}  {target_alias(target)}/{agent}"
                    )
                text = "Agents\n" + "\n".join(lines)
        elif command in {"help", "start"}:
            text = (
                "HerdRelay Telegram (read-only)\n"
                "/agents — list allowlisted agents\n"
                "/help — show this message\n"
                "Prompt submission and interrupt are not enabled yet."
            )
        else:
            text = (
                "Telegram is currently read-only. Use /agents or /help; "
                "no Core mutation was attempted."
            )
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )

    def _load_offset(self) -> int | None:
        value = self.store.get_state(OFFSET_STATE_KEY)
        if value is None:
            return None
        try:
            offset = int(value)
        except ValueError:
            LOG.warning("Ignoring invalid Telegram update offset %r", value)
            return None
        return offset if offset >= 0 else None

    def _save_offset(self, offset: int) -> None:
        self.store.set_state(OFFSET_STATE_KEY, str(offset))

    async def _sleep_reconnect(self) -> None:
        try:
            await asyncio.wait_for(
                self._stopped.wait(),
                timeout=self.telegram.reconnect_delay_seconds,
            )
        except TimeoutError:
            pass


def _update_id(update: Any) -> int:
    if not isinstance(update, dict):
        raise ValueError("Telegram update must be an object")
    value = update.get("update_id")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Telegram update_id must be an integer")
    return value
