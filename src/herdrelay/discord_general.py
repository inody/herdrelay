from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import time
from typing import Any, Protocol

import discord

from .config import AppConfig
from .domain import MutationOutcomeUnknownError, TargetKey
from .formatter import target_route_name, truncate
from .models import TransportAuditEntry
from .orca_bot_gateway import GatewayQueue
from .runtime import RuntimeClient
from .store import Store
from .telegram_router import (
    IntentModel,
    RecentRoutingContext,
    RouterCandidate,
    RoutingRequest,
    build_intent_model,
)

LOG = logging.getLogger(__name__)


class DiscordGeneralHandler(Protocol):
    async def handle(self, message: discord.Message) -> bool: ...

    def clear_context(self, message: discord.Message) -> bool: ...


class DiscordGeneralRouter:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
        model: IntentModel | None = None,
    ):
        self.config = config
        self.router = config.telegram.general_router
        self.store = store
        self.client = client
        self.model = model or build_intent_model(self.router.model)

    async def handle(self, message: discord.Message) -> bool:
        utterance = (message.content or "").strip()
        if not utterance or len(utterance) > self.config.max_message_chars:
            await _reply(message, "The input is empty or too long. Nothing was sent.")
            return False
        candidates = await self._candidates(message)
        if not candidates:
            await _reply(message, "No Agent threads are available. Nothing was sent.")
            return False
        recent_context = self._recent_context(message, candidates)
        request = RoutingRequest(
            utterance=utterance,
            candidates=candidates,
            source_kind="text",
            recent_context=recent_context,
        )
        try:
            decision = await self.model.classify(request)
        except Exception as exc:
            LOG.warning("Discord General router failed: %s", exc)
            self._audit(message, outcome="failed", detail=str(exc), payload=utterance)
            await _reply(message, "The destination could not be resolved. Nothing was sent.")
            return False

        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        selected = by_id.get(decision.candidate_id or "")
        prompt = decision.message_verbatim or ""
        valid_context_choice = (
            decision.routing_basis != "recent_context"
            or (
                recent_context is not None
                and selected is not None
                and selected.candidate_id == recent_context.candidate.candidate_id
            )
        )
        if (
            decision.intent != "send_prompt"
            or decision.ambiguous
            or decision.routing_basis not in {"explicit_destination", "recent_context"}
            or not valid_context_choice
            or selected is None
            or not prompt
            or prompt not in utterance
        ):
            self._audit(
                message,
                outcome="rejected",
                detail=decision.reason or "ambiguous or invalid routing decision",
                payload=utterance,
            )
            await _reply(
                message,
                "A single destination could not be selected. Nothing was sent.\n"
                + _candidate_summary(candidates),
            )
            return False

        self._audit(
            message,
            outcome="succeeded",
            detail=(
                f"provider={self.model.provider_name} "
                f"model={self.model.model_name or '-'} candidate={selected.candidate_id} "
                f"basis={decision.routing_basis}"
            ),
            payload=utterance,
            target_key=selected.key.to_storage_key(),
        )
        claimed = await self._send_prompt(message, selected=selected, text=prompt)
        if claimed:
            self._save_context(message, selected)
            await _reply(
                message,
                f"→ {selected.display_name}\n{truncate(prompt, max_chars=300)}",
            )
        return claimed

    async def _send_prompt(
        self, message: discord.Message, *, selected: RouterCandidate, text: str
    ) -> bool:
        blocked = _blocked_text(self.config, text)
        event_key = f"discord:message:{message.id}"
        audit_id = self.store.claim_transport_mutation(
            event_key=event_key,
            entry=TransportAuditEntry(
                transport="discord",
                actor_id=str(message.author.id),
                container_id=str(message.guild.id if message.guild else 0),
                conversation_id=str(message.channel.id),
                action="send_text",
                target_key=selected.key.to_storage_key(),
                payload_preview=truncate(text, max_chars=200),
                outcome="attempt",
            ),
        )
        if audit_id is None:
            return True
        if blocked is not None:
            self.store.finish_transport_audit(
                audit_id, outcome="rejected", detail="text blocklist"
            )
            await _reply(message, "Message was rejected by the text blocklist.")
            return True
        try:
            target = await asyncio.to_thread(
                self.client.resolve_target_key, selected.key
            )
            live_key = self.client.target_key(target.target)
            if live_key.to_route_value() != selected.key.to_route_value():
                raise ValueError("resolved target identity changed")
            await asyncio.to_thread(self.client.send, target.target, text)
        except MutationOutcomeUnknownError as exc:
            self.store.finish_transport_audit(
                audit_id, outcome="outcome_unknown", detail=truncate(str(exc), max_chars=500)
            )
            await _react(message, "⚠️")
            return True
        except Exception as exc:
            self.store.finish_transport_audit(
                audit_id, outcome="failed", detail=truncate(str(exc), max_chars=500)
            )
            await _react(message, "⚠️")
            return True
        self.store.finish_transport_audit(audit_id, outcome="succeeded")
        await _react(message, "✅")
        return True

    async def _candidates(
        self, message: discord.Message
    ) -> tuple[RouterCandidate, ...]:
        targets = await asyncio.to_thread(self.client.list_targets)
        bindings = self.store.list_bindings(
            guild_id=message.guild.id if message.guild else 0
        )
        candidates: list[RouterCandidate] = []
        for target in targets:
            if not target.agent_name:
                continue
            try:
                key = self.client.target_key(target.target)
            except Exception:
                continue
            binding = next(
                (
                    item
                    for item in bindings
                    if item.herdr_target == key.to_route_value()
                    and item.thread_id is not None
                    and int(item.channel_id) == self.config.thread_parent_channel_id
                ),
                None,
            )
            if binding is None:
                continue
            candidates.append(
                RouterCandidate(
                    candidate_id=f"candidate-{len(candidates) + 1}",
                    key=key,
                    target=target,
                    conversation_id=binding.thread_id,
                    name=target_route_name(key, target),
                )
            )
        return tuple(candidates)

    def clear_context(self, message: discord.Message) -> bool:
        if self.router.context_ttl_seconds <= 0:
            return False
        key = self._context_key(message)
        existed = self.store.get_state(key) is not None
        self.store.delete_state(key)
        return existed

    def _recent_context(
        self, message: discord.Message, candidates: tuple[RouterCandidate, ...]
    ) -> RecentRoutingContext | None:
        ttl = self.router.context_ttl_seconds
        if ttl <= 0:
            return None
        key = self._context_key(message)
        value = self.store.get_state(key)
        if value is None:
            return None
        try:
            raw = json.loads(value)
            route_target = str(raw["route_target"])
            saved_at = float(raw["saved_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.store.delete_state(key)
            return None
        age = time.time() - saved_at
        if age < 0 or age > ttl:
            self.store.delete_state(key)
            return None
        candidate = next(
            (
                item
                for item in candidates
                if item.key.to_route_value() == route_target
            ),
            None,
        )
        if candidate is None:
            self.store.delete_state(key)
            return None
        return RecentRoutingContext(candidate=candidate, age_seconds=int(age))

    def _save_context(self, message: discord.Message, candidate: RouterCandidate) -> None:
        if self.router.context_ttl_seconds <= 0:
            return
        self.store.set_state(
            self._context_key(message),
            json.dumps(
                {
                    "v": 1,
                    "route_target": candidate.key.to_route_value(),
                    "saved_at": time.time(),
                },
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _context_key(message: discord.Message) -> str:
        guild_id = message.guild.id if message.guild else 0
        return f"discord:general-context:{guild_id}:{message.channel.id}:{message.author.id}"

    def _audit(
        self,
        message: discord.Message,
        *,
        outcome: str,
        detail: str,
        payload: str,
        target_key: str | None = None,
    ) -> None:
        self.store.add_transport_audit(
            TransportAuditEntry(
                transport="discord",
                actor_id=str(message.author.id),
                container_id=str(message.guild.id if message.guild else 0),
                conversation_id=str(message.channel.id),
                action="route_text",
                target_key=target_key,
                payload_preview=truncate(payload, max_chars=200),
                outcome=outcome,
                detail=truncate(detail, max_chars=500),
            )
        )


class DiscordOrcaBotGeneral:
    def __init__(self, *, config: AppConfig, store: Store, queue: GatewayQueue):
        self.config = config
        self.store = store
        self.queue = queue
        self.bot = config.telegram.general_router.orca_bot
        self.client: discord.Client | None = None

    async def handle(self, message: discord.Message) -> bool:
        text = (message.content or "").strip()
        if not text or len(text) > self.config.max_message_chars:
            await _reply(message, "The input is empty or too long. Nothing was sent.")
            return False
        if _blocked_text(self.config, text) is not None:
            await _reply(message, "Message was rejected by the text blocklist.")
            return False
        target_key = f"orca-bot:{self.bot.bot_id}:discord"
        audit_id = self.store.claim_transport_mutation(
            event_key=f"discord:message:{message.id}",
            entry=TransportAuditEntry(
                transport="discord",
                actor_id=str(message.author.id),
                container_id=str(message.guild.id if message.guild else 0),
                conversation_id=str(message.channel.id),
                action="bot_send",
                target_key=target_key,
                payload_preview=truncate(text, max_chars=200),
                outcome="attempt",
            ),
        )
        if audit_id is None:
            return True
        request = {
            "version": 2,
            "kind": "send",
            "transport": "discord",
            "request_id": f"discord-{message.id}",
            "audit_id": audit_id,
            "event_key": f"discord:message:{message.id}",
            "guild_id": message.guild.id if message.guild else 0,
            "channel_id": message.channel.id,
            "user_id": message.author.id,
            "message_id": message.id,
            "source_kind": "text",
            "text": text,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            await asyncio.to_thread(self.queue.enqueue, request)
        except Exception as exc:
            self.store.finish_transport_audit(
                audit_id, outcome="failed", detail=truncate(str(exc), max_chars=500)
            )
            await _reply(message, "⚠️ Failed to queue the Orca Bot request.")
            return True
        await _react(message, "👀")
        return True

    def clear_context(self, message: discord.Message) -> bool:
        self.queue.enqueue(
            {
                "version": 2,
                "kind": "reset",
                "transport": "discord",
                "request_id": f"discord-reset-{message.id}",
                "guild_id": message.guild.id if message.guild else 0,
                "channel_id": message.channel.id,
                "user_id": message.author.id,
                "message_id": message.id,
                "source_kind": "text",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    async def handle_gateway_event(self, event: dict[str, Any]) -> None:
        if self.client is None:
            raise RuntimeError("Discord client is not attached to General handler")
        channel_id = _required_int(event, "channel_id")
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Discord General destination is not sendable: {channel_id}")
        kind = _required_string(event, "kind")
        if kind == "sent":
            await _react_to_gateway_message(channel, event, "✅")
        elif kind in {"send_failed", "outcome_unknown"}:
            await _react_to_gateway_message(channel, event, "⚠️")
            text = (
                "⚠️ The Orca Bot send outcome is unknown. It was not retried."
                if kind == "outcome_unknown"
                else "⚠️ Failed to send the request to Orca Bot."
            )
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        elif kind == "reply":
            text = _required_string(event, "text")
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            self.store.add_transport_audit(
                TransportAuditEntry(
                    transport="discord",
                    actor_id=str(event.get("user_id") or "orca-bot"),
                    container_id=str(event.get("guild_id") or 0),
                    conversation_id=str(channel_id),
                    action="bot_reply",
                    target_key=f"orca-bot:{self.bot.bot_id}:discord",
                    payload_preview=truncate(text, max_chars=200),
                    outcome="succeeded",
                    detail=f"request_id={event.get('request_id')}",
                )
            )


def _candidate_summary(candidates: tuple[RouterCandidate, ...]) -> str:
    lines = ["Candidate agents:"]
    for candidate in candidates[:8]:
        lines.append(
            f"{candidate.candidate_id}: {candidate.display_name} "
            f"({candidate.target.status or 'unknown'})"
        )
    return "\n".join(lines)


def _blocked_text(config: AppConfig, text: str) -> str | None:
    lowered = text.casefold()
    return next(
        (
            item
            for item in config.dangerous_text_blocklist
            if item.casefold() in lowered
        ),
        None,
    )


async def _reply(message: discord.Message, text: str) -> None:
    await message.reply(
        text,
        allowed_mentions=discord.AllowedMentions.none(),
        mention_author=False,
    )


async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        LOG.warning("Could not react to Discord message %s", message.id)


async def _react_to_gateway_message(
    channel: Any, event: dict[str, Any], emoji: str
) -> None:
    message_id = event.get("message_id")
    fetch = getattr(channel, "fetch_message", None)
    if not isinstance(message_id, int) or isinstance(message_id, bool) or fetch is None:
        return
    try:
        source = await fetch(message_id)
        await source.add_reaction(emoji)
    except discord.HTTPException:
        LOG.warning("Could not react to Discord General message %s", message_id)


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
