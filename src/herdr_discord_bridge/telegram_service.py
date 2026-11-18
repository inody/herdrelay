from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import AppConfig
from .domain import MutationOutcomeUnknownError, TargetKey
from .formatter import target_alias, truncate
from .hc_client import HcClient
from .hc_events import HcEventStream, HcStatusTransition, HcTerminalState
from .hc_protocol import HcEventNotification
from .hc_rpc import HcRpcError
from .models import TransportAuditEntry
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramApiError, TelegramBotApi
from .telegram_updates import TelegramUpdatePolicy, parse_inbound_message

LOG = logging.getLogger(__name__)
OFFSET_STATE_KEY = "telegram_next_update_id"
TOPIC_NAME_LIMIT = 128


class TelegramReadOnlyService:
    """Authorized long polling and status-only Core forum delivery."""

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
        self.core_client = client if isinstance(client, HcClient) else None
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
        tasks = [asyncio.create_task(self._poll_forever())]
        if self.core_client is not None:
            tasks.append(asyncio.create_task(self._status_forever()))
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

    async def _status_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._status_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Telegram Core status stream failed; reconnecting")
                await self._sleep_reconnect()

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

    async def _status_once(self) -> None:
        if self.core_client is None:
            raise RuntimeError("Telegram status delivery requires HcClient")
        subscription = await HcEventStream(
            self.config.runtime.core
        ).connect()
        self.core_client.observe_core_identity(
            subscription.hello.core.id, subscription.hello.core.name
        )
        core_id = subscription.hello.core.id
        state = HcTerminalState(subscription.snapshot)
        await self._reconcile_topics(core_id, state)

        stop_task = asyncio.create_task(self._stopped.wait())
        notification_task: asyncio.Task | None = None
        try:
            while not self._stopped.is_set():
                notification_task = asyncio.create_task(
                    subscription.next_notification()
                )
                done, _pending = await asyncio.wait(
                    {notification_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    notification_task.cancel()
                    await asyncio.gather(notification_task, return_exceptions=True)
                    return
                notification = notification_task.result()
                notification_task = None
                transitions = state.apply(notification)
                for transition in transitions:
                    await self._deliver_status(core_id, state, transition)
                if isinstance(notification, HcEventNotification):
                    await self._handle_lifecycle(core_id, state, notification)
        finally:
            if notification_task is not None:
                notification_task.cancel()
                await asyncio.gather(notification_task, return_exceptions=True)
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await subscription.close()

    async def _reconcile_topics(
        self, core_id: str, state: HcTerminalState
    ) -> None:
        for terminal in state.terminals.values():
            if not self.core_client or not self.core_client.address_allowed(
                terminal.address
            ):
                continue
            target = self.core_client.project_terminal(terminal)
            if target.agent_name:
                await self._ensure_topic(core_id, terminal.address, target)

    async def _deliver_status(
        self,
        core_id: str,
        state: HcTerminalState,
        transition: HcStatusTransition,
    ) -> None:
        if transition.current_state.casefold() not in self.telegram.statuses:
            return
        if not self.core_client or not self.core_client.address_allowed(
            transition.terminal
        ):
            return
        terminal = state.terminals.get(transition.terminal)
        if terminal is None:
            return
        target = self.core_client.project_terminal(terminal)
        if not target.agent_name:
            return
        topic = await self._ensure_topic(core_id, transition.terminal, target)
        dedupe_key = (
            f"telegram:status:{core_id}:{transition.terminal}:"
            f"{state.seq}:{transition.current_state.casefold()}"
        )
        if self.store.has_event_key(dedupe_key):
            return
        icon = "🔴" if transition.current_state.casefold() == "blocked" else "✅"
        title = f"{target_alias(target)}/{target.agent_name}"
        text = f"{icon} {title}\n{transition.current_state.casefold()}"
        if terminal.status and terminal.status.message:
            text += f"\n{terminal.status.message}"
        await self.api.send_text(
            chat_id=int(topic.chat_id),
            message_thread_id=int(topic.thread_id),
            text=text,
        )
        self.store.mark_event_seen(dedupe_key)

    async def _handle_lifecycle(
        self,
        core_id: str,
        state: HcTerminalState,
        notification: HcEventNotification,
    ) -> None:
        address = notification.terminal
        if not address or not self.core_client:
            return
        if notification.kind in {"created", "restarted"}:
            terminal = state.terminals.get(address)
            if terminal and self.core_client.address_allowed(address):
                target = self.core_client.project_terminal(terminal)
                if target.agent_name:
                    await self._ensure_topic(core_id, address, target)
        elif notification.kind in {"finished", "removed"}:
            topic = self.store.get_telegram_topic(
                core_id=core_id, terminal_address=address
            )
            if topic and not topic.closed:
                await self.api.close_forum_topic(
                    chat_id=int(topic.chat_id),
                    message_thread_id=int(topic.thread_id),
                )
                self.store.set_telegram_topic_closed(
                    core_id=core_id, terminal_address=address, closed=True
                )

    async def _ensure_topic(self, core_id: str, address: str, target):
        chat_id = self.telegram.forum_chat_id
        if chat_id is None:
            raise RuntimeError("Telegram forum_chat_id is not configured")
        name = f"{target_alias(target)}/{target.agent_name} · {address}"[
            :TOPIC_NAME_LIMIT
        ]
        topic = self.store.get_telegram_topic(
            core_id=core_id, terminal_address=address
        )
        if topic is None or int(topic.chat_id) != chat_id:
            result = await self.api.create_forum_topic(chat_id=chat_id, name=name)
            thread_id = result.get("message_thread_id")
            if not isinstance(thread_id, int) or isinstance(thread_id, bool):
                raise TelegramApiError(
                    "Telegram createForumTopic did not return message_thread_id"
                )
            self.store.upsert_telegram_topic(
                core_id=core_id,
                terminal_address=address,
                chat_id=chat_id,
                thread_id=thread_id,
                name=name,
            )
            topic = self.store.get_telegram_topic(
                core_id=core_id, terminal_address=address
            )
            assert topic is not None
            return topic
        if topic.closed:
            await self.api.reopen_forum_topic(
                chat_id=chat_id, message_thread_id=int(topic.thread_id)
            )
            self.store.set_telegram_topic_closed(
                core_id=core_id, terminal_address=address, closed=False
            )
        if topic.name != name:
            await self.api.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=int(topic.thread_id),
                name=name,
            )
            self.store.upsert_telegram_topic(
                core_id=core_id,
                terminal_address=address,
                chat_id=chat_id,
                thread_id=topic.thread_id,
                name=name,
                closed=False,
            )
        refreshed = self.store.get_telegram_topic(
            core_id=core_id, terminal_address=address
        )
        assert refreshed is not None
        return refreshed

    async def _handle_update(
        self, update: dict[str, Any], *, event_key: str
    ) -> bool:
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
            text = (
                "HerdRelay Telegram\n"
                "/agents — list allowlisted agents\n"
                "/help — show this message\n"
                f"{prompt_help}\nInterrupt is not enabled yet."
            )
        elif command is not None:
            text = "Unknown command. Use /agents or /help."
        elif self.telegram.enable_send:
            return await self._send_prompt(message, event_key=event_key)
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
        return False

    async def _send_prompt(self, message, *, event_key: str) -> bool:
        if self.core_client is None:
            await self._feedback(message, "Core runtime is unavailable.")
            return False
        if message.message_thread_id is None:
            await self._feedback(
                message, "Send prompts inside a bound agent forum topic."
            )
            return False
        route = self.store.get_telegram_topic_by_destination(
            chat_id=message.chat_id, thread_id=message.message_thread_id
        )
        if route is None or route.closed:
            await self._feedback(message, "This forum topic is not bound to a live agent.")
            return False
        text = message.text.strip()
        if len(text) > self.config.max_message_chars:
            await self._feedback(
                message,
                f"Message exceeds the {self.config.max_message_chars}-character limit.",
            )
            return False
        lowered = text.casefold()
        blocked = next(
            (
                item
                for item in self.config.dangerous_text_blocklist
                if item.casefold() in lowered
            ),
            None,
        )
        if blocked is not None:
            await self._feedback(message, "Message was rejected by the text blocklist.")
            return False

        targets = await asyncio.to_thread(self.core_client.list_targets)
        if self.core_client.core_id != route.core_id:
            await self._feedback(
                message,
                "This topic belongs to a different Core instance; no prompt was sent.",
            )
            return False
        target = next(
            (item for item in targets if item.target == route.terminal_address),
            None,
        )
        if target is None or not target.agent_name:
            await self._feedback(message, "The bound Core agent is not currently live.")
            return False

        target_key = TargetKey.core(
            controller_core_id=route.core_id,
            address=route.terminal_address,
        ).to_storage_key()
        audit_id = self.store.claim_transport_mutation(
            event_key=event_key,
            entry=TransportAuditEntry(
                transport="telegram",
                actor_id=str(message.user_id),
                container_id=str(message.chat_id),
                conversation_id=str(message.message_thread_id),
                action="send_text",
                target_key=target_key,
                payload_preview=truncate(text, max_chars=200),
                outcome="attempt",
            ),
        )
        if audit_id is None:
            return True

        try:
            receipt = await asyncio.to_thread(
                self.core_client.send_with_receipt,
                route.terminal_address,
                text,
            )
        except MutationOutcomeUnknownError as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="outcome_unknown",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(
                message,
                "⚠️ Prompt outcome is unknown. Inspect the agent before retrying.",
            )
        except HcRpcError as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="rejected" if exc.kind else "failed",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(message, f"⚠️ Prompt rejected: {exc}")
        except Exception as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="failed",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(message, "⚠️ Prompt submission failed.")
        else:
            self.store.finish_transport_audit(
                audit_id,
                outcome="succeeded",
                detail=truncate(str(receipt.result), max_chars=500),
                observed_turn=receipt.observed_turn,
            )
            await self._safe_feedback(message, "✅ Prompt submitted.")
        return True

    async def _feedback(self, message, text: str) -> None:
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )

    async def _safe_feedback(self, message, text: str) -> None:
        try:
            await self._feedback(message, text)
        except Exception:
            LOG.exception("Failed to send Telegram mutation feedback")

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
