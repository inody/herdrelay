from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta

from .config import AppConfig
from .domain import MutationOutcomeUnknownError, RuntimeKind, TargetKey
from .formatter import target_alias, truncate
from .hc_client import HcClient
from .hc_rpc import HcRpcError
from .herdr_cli import HerdrCliError
from .models import TransportAuditEntry
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_updates import TelegramUpdatePolicy

LOG = logging.getLogger(__name__)


class TelegramMutationHandler:
    """Claim, audit, and execute authorized Telegram runtime mutations."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
        api: TelegramBotApi,
    ):
        self.config = config
        self.telegram = config.telegram
        self.store = store
        self.client = client
        self.core_client = client if isinstance(client, HcClient) else None
        self.api = api
        self.policy = TelegramUpdatePolicy(self.telegram)

    async def request_interrupt(self, message) -> None:
        if self.core_client is None or message.message_thread_id is None:
            await self._feedback(
                message,
                "Interrupt is available only for a bound Herdr Core agent topic.",
            )
            return
        route = self.store.get_telegram_topic_by_destination(
            chat_id=message.chat_id, thread_id=message.message_thread_id
        )
        if route is None or route.closed:
            await self._feedback(message, "This topic is not bound to a live agent.")
            return
        targets = await asyncio.to_thread(self.core_client.list_targets)
        if self.core_client.core_id != route.core_id:
            await self._feedback(message, "This topic belongs to a different Core.")
            return
        target = next(
            (item for item in targets if item.target == route.terminal_address),
            None,
        )
        state = target.status.casefold() if target and target.status else "unknown"
        if target is None or not target.agent_name or state not in {"working", "blocked"}:
            await self._feedback(
                message, f"The bound agent is not interruptible in state {state}."
            )
            return

        token = secrets.token_urlsafe(12)
        self.store.create_telegram_confirmation(
            token=token,
            actor_id=message.user_id,
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            core_id=route.core_id,
            terminal_address=route.terminal_address,
            action="interrupt",
            expires_at=time.time() + self.telegram.confirmation_ttl_seconds,
        )
        await self.api.send_message(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=f"Interrupt {target_alias(target)}/{target.agent_name}?",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Interrupt",
                            "callback_data": f"interrupt:{token}",
                        },
                        {"text": "Cancel", "callback_data": f"cancel:{token}"},
                    ]
                ]
            },
        )

    async def handle_callback(self, callback, *, event_key: str) -> bool:
        if not self.policy.authorized(callback):
            return False
        action, separator, token = callback.data.partition(":")
        if not separator or action not in {"interrupt", "cancel"} or not token:
            await self._safe_answer_callback(
                callback, "Unsupported or stale action.", show_alert=True
            )
            return False
        confirmation = self.store.consume_telegram_confirmation(
            token=token,
            actor_id=callback.user_id,
            chat_id=callback.chat_id,
            thread_id=callback.message_thread_id or 0,
            action="interrupt",
        )
        if confirmation is None:
            self._audit_interrupt_rejection(
                callback, detail="stale, expired, consumed, or mismatched confirmation"
            )
            await self._safe_answer_callback(
                callback, "This confirmation is stale or already used.", show_alert=True
            )
            return False
        if action == "cancel":
            self._audit_interrupt_rejection(
                callback,
                detail="cancelled by actor",
                target_key=TargetKey.core(
                    controller_core_id=confirmation.core_id,
                    address=confirmation.terminal_address,
                ).to_storage_key(),
            )
            await self._safe_answer_callback(callback, "Cancelled.")
            return False
        return await self._execute_interrupt(
            callback, confirmation=confirmation, event_key=event_key
        )

    async def _execute_interrupt(
        self, callback, *, confirmation, event_key: str
    ) -> bool:
        if self.core_client is None or callback.message_thread_id is None:
            self._audit_interrupt_rejection(callback, detail="Core runtime unavailable")
            await self._safe_answer_callback(
                callback, "Core runtime is unavailable.", show_alert=True
            )
            return False
        route = self.store.get_telegram_topic_by_destination(
            chat_id=callback.chat_id, thread_id=callback.message_thread_id
        )
        expected = (confirmation.core_id, confirmation.terminal_address)
        actual = (
            (route.core_id, route.terminal_address)
            if route is not None and not route.closed
            else None
        )
        target_key = TargetKey.core(
            controller_core_id=confirmation.core_id,
            address=confirmation.terminal_address,
        ).to_storage_key()
        if actual != expected:
            self._audit_interrupt_rejection(
                callback, detail="topic route changed", target_key=target_key
            )
            await self._safe_answer_callback(
                callback, "This topic binding changed.", show_alert=True
            )
            return False

        targets = await asyncio.to_thread(self.core_client.list_targets)
        target = next(
            (item for item in targets if item.target == confirmation.terminal_address),
            None,
        )
        state = target.status.casefold() if target and target.status else "unknown"
        if (
            self.core_client.core_id != confirmation.core_id
            or target is None
            or not target.agent_name
            or state not in {"working", "blocked"}
        ):
            self._audit_interrupt_rejection(
                callback,
                detail=f"target is not interruptible in state {state}",
                target_key=target_key,
            )
            await self._safe_answer_callback(
                callback,
                f"Agent is not interruptible in state {state}.",
                show_alert=True,
            )
            return False

        rate_limit = self._mutation_rate_limit(
            actor_id=str(callback.user_id),
            container_id=str(callback.chat_id),
            target_key=target_key,
        )
        if rate_limit is not None:
            self._audit_interrupt_rejection(
                callback, detail=rate_limit, target_key=target_key
            )
            await self._safe_answer_callback(
                callback, "Interrupt rate limit exceeded.", show_alert=True
            )
            return False

        audit_id = self.store.claim_transport_mutation(
            event_key=event_key,
            entry=TransportAuditEntry(
                transport="telegram",
                actor_id=str(callback.user_id),
                container_id=str(callback.chat_id),
                conversation_id=str(callback.message_thread_id),
                action="interrupt",
                target_key=target_key,
                outcome="attempt",
            ),
        )
        if audit_id is None:
            return True
        await self._safe_answer_callback(callback, "Interrupting…")
        try:
            receipt = await asyncio.to_thread(
                self.core_client.interrupt_with_receipt,
                confirmation.terminal_address,
            )
        except MutationOutcomeUnknownError as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="outcome_unknown",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(
                callback,
                "⚠️ Interrupt outcome is unknown. Inspect the agent before retrying.",
            )
        except HcRpcError as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="rejected" if exc.kind else "failed",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(callback, f"⚠️ Interrupt rejected: {exc}")
        except Exception as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="failed",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._safe_feedback(callback, "⚠️ Interrupt failed.")
        else:
            self.store.finish_transport_audit(
                audit_id,
                outcome="succeeded",
                detail=truncate(str(receipt.result), max_chars=500),
                observed_turn=receipt.observed_turn,
            )
            await self._safe_feedback(callback, "✅ Interrupt sent.")
        return True

    def _audit_interrupt_rejection(
        self, callback, *, detail: str, target_key: str | None = None
    ) -> None:
        self.store.add_transport_audit(
            TransportAuditEntry(
                transport="telegram",
                actor_id=str(callback.user_id),
                container_id=str(callback.chat_id),
                conversation_id=(
                    str(callback.message_thread_id)
                    if callback.message_thread_id is not None
                    else None
                ),
                action="interrupt",
                target_key=target_key,
                outcome="rejected",
                detail=truncate(detail, max_chars=500),
            )
        )

    async def _safe_answer_callback(
        self, callback, text: str, *, show_alert: bool = False
    ) -> None:
        try:
            await self.api.answer_callback_query(
                callback_query_id=callback.callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        except Exception:
            LOG.exception("Failed to answer Telegram callback query")

    async def send_prompt(self, message, *, event_key: str) -> bool:
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

        key: TargetKey | None = None
        try:
            key = TargetKey.from_route_value(route.route_target)
            targets = await asyncio.to_thread(self.client.list_targets)
            target = next(
                (item for item in targets if item.target == key.address), None
            )
            if target is None or self.client.target_key(target.target) != key:
                raise ValueError("topic target identity changed")
        except Exception:
            detail = (
                "This topic belongs to a different Core instance; no prompt was sent."
                if key is not None and key.runtime is RuntimeKind.HERDR_CORE
                else "The bound runtime agent is not currently live."
            )
            await self._feedback(message, detail)
            return False
        assert key is not None
        if not target.agent_name:
            await self._feedback(
                message, "The bound runtime target has no detected agent."
            )
            return False

        target_key = key.to_storage_key()
        rate_limit = self._mutation_rate_limit(
            actor_id=str(message.user_id),
            container_id=str(message.chat_id),
            target_key=target_key,
        )
        if rate_limit is not None:
            self.store.add_transport_audit(
                TransportAuditEntry(
                    transport="telegram",
                    actor_id=str(message.user_id),
                    container_id=str(message.chat_id),
                    conversation_id=str(message.message_thread_id),
                    action="send_text",
                    target_key=target_key,
                    payload_preview=truncate(text, max_chars=200),
                    outcome="rejected",
                    detail=rate_limit,
                )
            )
            await self._feedback(message, "Mutation rate limit exceeded.")
            return False
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
            receipt = None
            if self.core_client is not None and key.runtime is RuntimeKind.HERDR_CORE:
                receipt = await asyncio.to_thread(
                    self.core_client.send_with_receipt,
                    target.target,
                    text,
                )
            else:
                await asyncio.to_thread(self.client.send, target.target, text)
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
        except HerdrCliError as exc:
            outcome = (
                "outcome_unknown"
                if "timed out" in str(exc).casefold()
                else "failed"
            )
            self.store.finish_transport_audit(
                audit_id,
                outcome=outcome,
                detail=truncate(str(exc), max_chars=500),
            )
            feedback = (
                "⚠️ Prompt outcome is unknown. Inspect the agent before retrying."
                if outcome == "outcome_unknown"
                else "⚠️ Prompt submission failed."
            )
            await self._safe_feedback(message, feedback)
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
                detail=(
                    truncate(str(receipt.result), max_chars=500)
                    if receipt is not None
                    else None
                ),
                observed_turn=(receipt.observed_turn if receipt is not None else None),
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

    def _mutation_rate_limit(
        self, *, actor_id: str, container_id: str, target_key: str
    ) -> str | None:
        since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        checks = (
            (
                "actor",
                self.telegram.actor_mutations_per_minute,
                {"actor_id": actor_id},
            ),
            (
                "chat",
                self.telegram.chat_mutations_per_minute,
                {"container_id": container_id},
            ),
            (
                "target",
                self.telegram.target_mutations_per_minute,
                {"target_key": target_key},
            ),
        )
        for label, limit, key in checks:
            count = self.store.count_transport_mutations(
                transport="telegram", since=since, **key
            )
            if count >= limit:
                return f"rate limit: {label} has {count} mutations in the last minute"
        return None

