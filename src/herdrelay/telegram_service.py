from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import AppConfig, effective_general_provider
from .formatter import target_alias
from .orca_bot_gateway import GatewayQueue
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler
from .telegram_orca_bot import (
    OrcaBotGatewaySupervisor,
    TelegramOrcaBotGeneral,
)
from .telegram_output import TelegramOutputRelay
from .telegram_router import IntentModel, TelegramGeneralRouter
from .telegram_status import TelegramStatusRelay
from .telegram_topics import TelegramTopicManager
from .telegram_updates import (
    TelegramUpdatePolicy,
    parse_inbound_callback,
    parse_inbound_message,
)
from .telegram_voice import TelegramVoiceHandler
from .voice_transcription import VoiceTranscriber

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
        router_model: IntentModel | None = None,
        voice_transcriber: VoiceTranscriber | None = None,
        gateway_queue: GatewayQueue | None = None,
        supervise_gateway: bool = True,
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
        self.orca_bot_gateway = None
        if not self.telegram.general_router.enabled:
            self.general_router = None
        elif effective_general_provider(config) == "orca-bot":
            queue = gateway_queue or GatewayQueue(
                self.telegram.general_router.orca_bot.queue_path
            )
            self.general_router = TelegramOrcaBotGeneral(
                config=config,
                store=store,
                api=self.api,
                mutations=self.mutations,
                queue=queue,
            )
            if supervise_gateway:
                self.orca_bot_gateway = OrcaBotGatewaySupervisor(
                    config=config,
                    store=store,
                    stopped=self._stopped,
                    queue=queue,
                    deliver_event=self.general_router.handle_gateway_event,
                )
        else:
            self.general_router = TelegramGeneralRouter(
                config=config,
                store=store,
                client=client,
                api=self.api,
                mutations=self.mutations,
                model=router_model,
            )
        self.voice_handler = (
            TelegramVoiceHandler(
                config=config,
                store=store,
                api=self.api,
                router=self.general_router,
                transcriber=voice_transcriber,
            )
            if self.telegram.voice_input.enabled and self.general_router is not None
            else None
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
        if self.orca_bot_gateway is not None:
            tasks.append(
                asyncio.create_task(self.orca_bot_gateway.run_forever())
            )
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
        if message.voice is not None:
            if self.voice_handler is not None:
                return await self.voice_handler.handle(message, event_key=event_key)
            await self.api.send_text(
                chat_id=message.chat_id,
                message_thread_id=message.message_thread_id,
                text="Voice input is disabled. Nothing was sent.",
            )
            return False
        command = message.command
        if command in {"agents", "list"}:
            targets = await asyncio.to_thread(self.client.list_targets)
            if not targets:
                text = "No agents are currently available."
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
            if self.telegram.general_router.enabled:
                if effective_general_provider(self.config) == "orca-bot":
                    prompt_help = (
                        "Plain text and voice in General are sent to the "
                        "configured Orca Bot; bound topics submit directly. "
                        "Use /forget to start a new General Bot conversation."
                    )
                else:
                    prompt_help = (
                        "Plain text in General is routed by the configured model; "
                        "bound topics submit directly. Use /forget to clear the "
                        "recent General destination."
                    )
            elif self.telegram.enable_send:
                prompt_help = "Plain text in a bound topic submits a prompt."
            else:
                prompt_help = "Prompt submission is disabled."
            interrupt_help = (
                "/interrupt — request a one-time confirmation button"
                if self.telegram.enable_interrupt
                else "Interrupt is disabled."
            )
            text = (
                "HerdRelay Telegram\n"
                "/agents — list available agents\n"
                "/help — show this message\n"
                "/forget — clear the recent General destination\n"
                "//command — send /command to the agent\n"
                f"{prompt_help}\n{interrupt_help}"
            )
        elif command == "forget":
            if self.general_router is None:
                text = "General routing context is disabled."
            elif self.general_router.clear_context(message):
                text = (
                    "The General Orca Bot conversation was reset."
                    if effective_general_provider(self.config) == "orca-bot"
                    else "The recent General destination was cleared."
                )
            else:
                text = "No recent General destination is saved."
        elif command == "interrupt":
            if not self.telegram.enable_interrupt:
                text = "Telegram interrupt is disabled."
            else:
                await self.mutations.request_interrupt(message)
                return False
        elif command is not None:
            text = "Unknown command. Use /agents, /forget, /interrupt, or /help."
        elif self.general_router is not None and self.general_router.accepts(message):
            return await self.general_router.handle(message, event_key=event_key)
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
