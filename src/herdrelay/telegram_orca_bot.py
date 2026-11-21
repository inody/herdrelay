from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .config import AppConfig
from .formatter import truncate
from .models import TransportAuditEntry
from .orca_bot_gateway import GatewayQueue, _read_json, run_orca_json
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler
from .telegram_updates import TelegramInboundMessage

LOG = logging.getLogger(__name__)
TERMINAL_CREATE_COOLDOWN_SECONDS = 60


class TelegramOrcaBotGeneral:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        api: TelegramBotApi,
        mutations: TelegramMutationHandler,
        queue: GatewayQueue,
    ):
        self.config = config
        self.telegram = config.telegram
        self.general = config.telegram.general_router
        self.bot = self.general.orca_bot
        self.store = store
        self.api = api
        self.mutations = mutations
        self.queue = queue

    def accepts(self, message: TelegramInboundMessage) -> bool:
        return message.message_thread_id is None or (
            self.general.general_topic_id is not None
            and message.message_thread_id == self.general.general_topic_id
        )

    async def handle(self, message: TelegramInboundMessage, *, event_key: str) -> bool:
        text = message.prompt_text
        if not text or len(text) > self.config.max_message_chars:
            await self._feedback(
                message, "The input is empty or too long. Nothing was sent."
            )
            return False
        blocked = self.mutations.blocked_text(text)
        if blocked is not None:
            await self._feedback(message, "Message was rejected by the text blocklist.")
            return False

        target_key = f"orca-bot:{self.bot.bot_id}"
        rate_limit = self.mutations.mutation_rate_limit(
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
                    conversation_id=_thread_value(message.message_thread_id),
                    action="bot_send",
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
                conversation_id=_thread_value(message.message_thread_id),
                action="bot_send",
                target_key=target_key,
                payload_preview=truncate(text, max_chars=200),
                outcome="attempt",
            ),
        )
        if audit_id is None:
            return True
        request_id = f"telegram-{message.update_id}"
        request = {
            "version": 2,
            "kind": "send",
            "transport": "telegram",
            "request_id": request_id,
            "audit_id": audit_id,
            "event_key": event_key,
            "chat_id": message.chat_id,
            "thread_id": message.message_thread_id,
            "user_id": message.user_id,
            "message_id": message.message_id,
            "source_kind": message.source_kind,
            "text": text,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            await asyncio.to_thread(self.queue.enqueue, request)
        except Exception as exc:
            self.store.finish_transport_audit(
                audit_id,
                outcome="failed",
                detail=truncate(str(exc), max_chars=500),
            )
            await self._feedback(message, "⚠️ Failed to queue the Orca Bot request.")
            return True
        await self.mutations.safe_reaction(message, "👀")
        return True

    def clear_context(self, message: TelegramInboundMessage) -> bool:
        request_id = f"reset-{message.update_id}"
        self.queue.enqueue(
            {
                "version": 2,
                "kind": "reset",
                "transport": "telegram",
                "request_id": request_id,
                "chat_id": message.chat_id,
                "thread_id": message.message_thread_id,
                "user_id": message.user_id,
                "message_id": message.message_id,
                "source_kind": message.source_kind,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    async def handle_gateway_event(self, event: dict[str, Any]) -> None:
        chat_id = _required_int(event, "chat_id")
        if chat_id not in self.telegram.allowed_chat_ids:
            LOG.warning(
                "Discarding Orca Bot event for unauthorized Telegram chat %s",
                chat_id,
            )
            return
        kind = _required_string(event, "kind")
        if kind == "sent":
            try:
                await self.api.set_message_reaction(
                    chat_id=_required_int(event, "chat_id"),
                    message_id=_required_int(event, "message_id"),
                    emoji="✅",
                )
            except Exception:
                LOG.exception("Failed to confirm Telegram Orca Bot send")
        elif kind in {"send_failed", "outcome_unknown"}:
            await self.api.send_text(
                chat_id=_required_int(event, "chat_id"),
                message_thread_id=_optional_int(event.get("thread_id")),
                text=(
                    "⚠️ The Orca Bot send outcome is unknown. It was not retried."
                    if kind == "outcome_unknown"
                    else "⚠️ Failed to send the request to Orca Bot."
                ),
            )
        elif kind == "reply":
            text = _required_string(event, "text")
            await self.api.send_text(
                chat_id=_required_int(event, "chat_id"),
                message_thread_id=_optional_int(event.get("thread_id")),
                text=text,
            )
            self.store.add_transport_audit(
                TransportAuditEntry(
                    transport="telegram",
                    actor_id=str(event.get("user_id") or "orca-bot"),
                    container_id=str(event.get("chat_id")),
                    conversation_id=_thread_value(
                        _optional_int(event.get("thread_id"))
                    ),
                    action="bot_reply",
                    target_key=f"orca-bot:{self.bot.bot_id}",
                    payload_preview=truncate(text, max_chars=200),
                    outcome="succeeded",
                    detail=f"request_id={event.get('request_id')}",
                )
            )

    async def _feedback(self, message: TelegramInboundMessage, text: str) -> None:
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )


class OrcaBotGatewaySupervisor:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        stopped: asyncio.Event,
        queue: GatewayQueue,
        deliver_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        api: TelegramBotApi | None = None,
    ):
        self.config = config
        self.store = store
        self.stopped = stopped
        self.runtime = config.runtime.orca
        self.bot = config.telegram.general_router.orca_bot
        self.queue = queue
        self.api = api
        if deliver_event is None:
            if api is None:
                raise ValueError("deliver_event or api is required")
            self.deliver_event = self._deliver_legacy_telegram_event
        else:
            self.deliver_event = deliver_event
        self._next_create_at = 0.0
        self._stale_seen_at: float | None = None

    async def run_forever(self) -> None:
        await asyncio.to_thread(self.queue.prepare)
        while not self.stopped.is_set():
            try:
                await self.drain_events_once()
                await asyncio.to_thread(self.ensure_worker)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Orca Bot gateway supervision failed")
            try:
                await asyncio.wait_for(
                    self.stopped.wait(), timeout=self.bot.refresh_seconds
                )
            except TimeoutError:
                pass

    async def drain_events_once(self) -> None:
        paths = await asyncio.to_thread(self.queue.event_paths)
        for path in paths:
            try:
                event = await asyncio.to_thread(_read_json, path)
                await self._handle_event(path, event)
            except FileNotFoundError:
                continue
            except Exception:
                LOG.exception("Could not process Orca Bot gateway event %s", path.name)

    async def _handle_event(self, path: Path, event: dict[str, Any]) -> None:
        request_id = _required_string(event, "request_id")
        kind = _required_string(event, "kind")
        event_key = f"orca-bot-gateway:{path.name}"
        if self.store.has_event_key(event_key):
            path.unlink(missing_ok=True)
            return
        audit_id = event.get("audit_id")
        if kind == "sent":
            if isinstance(audit_id, int) and not isinstance(audit_id, bool):
                self.store.finish_transport_audit(
                    audit_id,
                    outcome="succeeded",
                    detail=truncate(
                        f"message_id={event.get('outbound_message_id')} "
                        f"chat_id={event.get('bot_chat_id')}",
                        max_chars=500,
                    ),
                )
        elif kind in {"send_failed", "outcome_unknown"}:
            outcome = "outcome_unknown" if kind == "outcome_unknown" else "failed"
            detail = str(event.get("detail") or "Orca Bot gateway send failed")
            if isinstance(audit_id, int) and not isinstance(audit_id, bool):
                self.store.finish_transport_audit(
                    audit_id,
                    outcome=outcome,
                    detail=truncate(detail, max_chars=500),
                )
        elif kind not in {"reply", "reset"}:
            raise ValueError(f"unknown Orca Bot gateway event kind: {kind}")
        await self.deliver_event(event)
        self.store.mark_event_seen(event_key)
        path.unlink(missing_ok=True)

    async def _deliver_legacy_telegram_event(self, event: dict[str, Any]) -> None:
        assert self.api is not None
        kind = _required_string(event, "kind")
        if kind == "sent":
            try:
                await self.api.set_message_reaction(
                    chat_id=_required_int(event, "chat_id"),
                    message_id=_required_int(event, "message_id"),
                    emoji="✅",
                )
            except Exception:
                LOG.exception("Failed to confirm Telegram Orca Bot send")
        elif kind in {"send_failed", "outcome_unknown"}:
            await self.api.send_text(
                chat_id=_required_int(event, "chat_id"),
                message_thread_id=_optional_int(event.get("thread_id")),
                text=(
                    "⚠️ The Orca Bot send outcome is unknown. It was not retried."
                    if kind == "outcome_unknown"
                    else "⚠️ Failed to send the request to Orca Bot."
                ),
            )
        elif kind == "reply":
            text = _required_string(event, "text")
            await self.api.send_text(
                chat_id=_required_int(event, "chat_id"),
                message_thread_id=_optional_int(event.get("thread_id")),
                text=text,
            )
            self.store.add_transport_audit(
                TransportAuditEntry(
                    transport="telegram",
                    actor_id=str(event.get("user_id") or "orca-bot"),
                    container_id=str(event.get("chat_id")),
                    conversation_id=_thread_value(
                        _optional_int(event.get("thread_id"))
                    ),
                    action="bot_reply",
                    target_key=f"orca-bot:{self.bot.bot_id}",
                    payload_preview=truncate(text, max_chars=200),
                    outcome="succeeded",
                    detail=f"request_id={event.get('request_id')}",
                )
            )

    def ensure_worker(self) -> None:
        heartbeat = self._heartbeat()
        if (
            heartbeat is not None
            and time.time() - heartbeat["updated_at"]
            <= self.bot.heartbeat_stale_seconds
        ):
            self._stale_seen_at = None
            return

        terminals = self._terminal_list()
        stale_terminals = self._stale_terminal_candidates(
            terminals=terminals,
            heartbeat=heartbeat,
        )
        if stale_terminals:
            self._mark_stale_or_wait()
            if self._stale_seen_at is not None:
                return
        self._stale_seen_at = None

        for terminal in stale_terminals:
            handle = terminal.get("handle")
            if isinstance(handle, str) and handle:
                self._terminal_close(handle)
        if time.monotonic() < self._next_create_at:
            return
        self._terminal_create()

    def _stale_terminal_candidates(
        self,
        *,
        terminals: list[dict[str, Any]],
        heartbeat: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if heartbeat is not None:
            handle = heartbeat["handle"]
            return [
                terminal
                for terminal in terminals
                if terminal.get("handle") == handle
                and terminal.get("worktreeId") == self.bot.gateway_worktree
            ]
        return [
            terminal
            for terminal in terminals
            if terminal.get("worktreeId") == self.bot.gateway_worktree
            and (
                terminal.get("paneTitle") == self.bot.gateway_title
                or terminal.get("title") == self.bot.gateway_title
            )
        ]

    def _mark_stale_or_wait(self) -> None:
        now = time.monotonic()
        if self._stale_seen_at is None:
            self._stale_seen_at = now
            return
        if now - self._stale_seen_at < self.bot.heartbeat_stale_seconds:
            return
        self._stale_seen_at = None

    def _heartbeat(self) -> dict[str, Any] | None:
        try:
            value = _read_json(self.queue.heartbeat)
        except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
            return None
        updated_at = value.get("updated_at")
        handle = value.get("handle")
        if (
            value.get("bot_id") != self.bot.bot_id
            or value.get("gateway_worktree") != self.bot.gateway_worktree
            or not isinstance(updated_at, (int, float))
            or isinstance(updated_at, bool)
            or not isinstance(handle, str)
            or not handle
        ):
            return None
        return {"updated_at": float(updated_at), "handle": handle}

    def _terminal_list(self) -> list[dict[str, Any]]:
        payload = self._orca_json(["terminal", "list", "--include-visual-layouts"])
        result = _object(payload.get("result"), "Orca terminal list result")
        terminals = result.get("terminals")
        if not isinstance(terminals, list):
            raise RuntimeError("Orca terminal list is missing terminals")
        return [terminal for terminal in terminals if isinstance(terminal, dict)]

    def _terminal_create(self) -> None:
        self._next_create_at = time.monotonic() + TERMINAL_CREATE_COOLDOWN_SECONDS
        try:
            payload = self._orca_json(
                [
                    "terminal",
                    "create",
                    "--worktree",
                    self.bot.gateway_worktree or "",
                    "--title",
                    self.bot.gateway_title,
                    "--command",
                    self.bot.gateway_command or "",
                ],
                mutation=True,
            )
        except Exception:
            LOG.exception("Could not create Orca Bot gateway terminal")
            return
        result = _object(payload.get("result"), "Orca terminal create result")
        terminal = _object(result.get("terminal"), "created Orca terminal")
        handle = _required_string(terminal, "handle")
        LOG.info("Created Orca Bot gateway terminal %s", handle)

    def _terminal_close(self, handle: str) -> None:
        try:
            self._orca_json(
                ["terminal", "close", "--terminal", handle], mutation=True
            )
        except Exception:
            LOG.exception("Could not close stale Orca Bot gateway terminal %s", handle)
            raise

    def _orca_json(
        self, args: list[str], *, mutation: bool = False
    ) -> dict[str, Any]:
        return run_orca_json(
            cli_path=self.runtime.cli_path,
            environment=self.runtime.environment,
            timeout_seconds=self.bot.command_timeout_seconds,
            args=args,
            mutation=mutation,
        )


def _thread_value(value: int | None) -> str | None:
    return str(value) if value is not None else None


def _required_string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("optional integer value is invalid")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value
