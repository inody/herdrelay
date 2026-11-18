from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import AppConfig
from .formatter import target_alias
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler
from .telegram_output import TelegramOutputRelay
from .telegram_status import TelegramStatusRelay
from .telegram_topics import TelegramTopicManager
from .telegram_updates import (
    TelegramUpdatePolicy,
    parse_inbound_callback,
    parse_inbound_message,
)

LOG = logging.getLogger(__name__)
OFFSET_STATE_KEY = "telegram_next_update_id"


class TelegramService:
    """Authorized long polling and runtime-neutral forum delivery."""

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
        self.topics = TelegramTopicManager(
            config=config, store=store, api=self.api
        )
        self.output_relay = (
            TelegramOutputRelay(config=config, store=store, api=self.api)
            if config.enable_streaming and config.streaming.mode == "hooks"
            else None
        )
        self._stopped = asyncio.Event()
        self.mutations = TelegramMutationHandler(
            config=config, store=store, client=client, api=self.api
        )
        self.status_relay = TelegramStatusRelay(
            config=config,
            store=store,
            client=client,
            api=self.api,
            stopped=self._stopped,
            topics=self.topics,
        )

    def stop(self) -> None:
        self._stopped.set()
        if self.output_relay is not None:
            self.output_relay.stop()

    async def close(self) -> None:
        self.stop()
        await self.api.close()

    async def run_forever(self) -> None:
        tasks = [
            asyncio.create_task(self._poll_forever()),
            asyncio.create_task(self.status_relay.run_forever()),
        ]
        if self.output_relay is not None:
            tasks.append(asyncio.create_task(self.output_relay.run_forever()))
        try:
            await self._stopped.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.api.close()

    async def _poll_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Telegram polling failed; reconnecting")
                await self._sleep_reconnect()

    async def poll_once(self) -> None:
        offset = self._load_offset()
        allowed_updates = (
            ("message", "callback_query")
            if self.telegram.enable_interrupt
            else ("message",)
        )
        updates = await self.api.get_updates(
            offset=offset,
            timeout_seconds=self.telegram.poll_timeout_seconds,
            allowed_updates=allowed_updates,
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
            claimed_mutation = False
            try:
                claimed_mutation = await self._handle_update(
                    update, event_key=dedupe_key
                )
            except ValueError as exc:
                LOG.warning("Discarding invalid Telegram update %s: %s", update_id, exc)
            if not claimed_mutation and not self.store.mark_event_seen(dedupe_key):
                LOG.info("Telegram update %s was already handled", update_id)
            self._save_offset(update_id + 1)

    async def _handle_update(
        self, update: dict[str, Any], *, event_key: str
    ) -> bool:
        # Tests and embedded callers may replace the immutable AppConfig.
        self.mutations.config = self.config
        self.mutations.telegram = self.config.telegram
        self.mutations.policy = TelegramUpdatePolicy(self.config.telegram)
        callback = parse_inbound_callback(update)
        if callback is not None:
            return await self.mutations.handle_callback(
                callback, event_key=event_key
            )
        message = parse_inbound_message(update)
        if message is None or not self.policy.authorized(message):
            return False
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
            prompt_help = (
                "Plain text in a bound topic submits a prompt."
                if self.telegram.enable_send
                else "Prompt submission is disabled."
            )
            interrupt_help = (
                "/interrupt — request a one-time confirmation button"
                if self.telegram.enable_interrupt
                else "Interrupt is disabled."
            )
            text = (
                "HerdRelay Telegram\n"
                "/agents — list allowlisted agents\n"
                "/help — show this message\n"
                f"{prompt_help}\n{interrupt_help}"
            )
        elif command == "interrupt":
            if not self.telegram.enable_interrupt:
                text = "Telegram interrupt is disabled."
            else:
                await self.mutations.request_interrupt(message)
                return False
        elif command is not None:
            text = "Unknown command. Use /agents, /interrupt, or /help."
        elif self.telegram.enable_send:
            return await self.mutations.send_prompt(
                message, event_key=event_key
            )
        else:
            text = (
                "Telegram is currently read-only. Use /agents or /help; "
                "no runtime mutation was attempted."
            )
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )
        return False

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
