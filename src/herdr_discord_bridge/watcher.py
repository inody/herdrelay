from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from .config import AppConfig
from .formatter import format_tail
from .herdr_client import HerdrClient
from .models import Binding
from .store import Store

LOG = logging.getLogger(__name__)

APPROVAL_VIEW_FACTORY = Callable[[str], discord.ui.View | None]


@dataclass(frozen=True)
class AgentStatusEvent:
    pane_id: str
    status: str
    previous_status: str | None = None
    agent_name: str | None = None
    raw: dict[str, Any] | None = None


class EventWatcher:
    def __init__(
        self,
        *,
        bot: discord.Client,
        config: AppConfig,
        store: Store,
        client: HerdrClient,
        card_view_factory: APPROVAL_VIEW_FACTORY | None = None,
    ):
        self.bot = bot
        self.config = config
        self.store = store
        self.client = client
        self.card_view_factory = card_view_factory
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Herdr event watcher crashed; reconnecting")
                await self._sleep_reconnect()

    async def _run_once(self) -> None:
        socket_path = resolve_socket_path(self.config)
        LOG.info("Connecting Herdr event watcher to %s", socket_path)
        reader, writer = await asyncio.open_unix_connection(socket_path)
        try:
            subscriptions = await asyncio.to_thread(
                build_agent_status_subscriptions, self.client, self.config
            )
            request = {
                "id": "discord_bridge_events",
                "method": "events.subscribe",
                "params": {"subscriptions": subscriptions},
            }
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()

            subscribed_at = time.monotonic()
            while not self._stopped.is_set():
                if should_resubscribe(subscribed_at, self.config):
                    LOG.info("Reconnecting Herdr event watcher to refresh subscriptions")
                    return
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=1)
                except TimeoutError:
                    continue
                if not line:
                    raise ConnectionError("Herdr event stream closed")
                await self._handle_line(line)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_line(self, line: bytes) -> None:
        try:
            payload = json.loads(line.decode())
        except json.JSONDecodeError:
            LOG.warning("Ignoring non-JSON Herdr event line")
            return
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"Herdr event stream error: {payload['error']}")
        if isinstance(payload, dict) and payload.get("id") == "discord_bridge_events":
            LOG.info("Herdr event watcher subscribed")
            return

        event = parse_agent_status_event(payload)
        if not event:
            return
        if event.status not in {status.casefold() for status in self.config.watcher.statuses}:
            return
        await self._notify(event)

    async def _notify(self, event: AgentStatusEvent) -> None:
        output = ""
        dedupe_material = ""
        if self.config.watcher.include_output:
            tail_lines = (
                self.config.watcher.blocked_tail_lines
                if event.status == "blocked"
                else self.config.watcher.done_tail_lines
            )
            try:
                output = await asyncio.to_thread(
                    self.client.read, event.pane_id, lines=tail_lines
                )
                dedupe_material = output
            except Exception as exc:
                LOG.warning("Failed to read tail for %s: %s", event.pane_id, exc)
                dedupe_material = f"read-error:{event.pane_id}:{event.status}"
        else:
            dedupe_material = await asyncio.to_thread(
                event_state_marker, self.client, event
            )

        dedupe_key = event_dedupe_key(event, dedupe_material)
        legacy_dedupe_prefix = event_legacy_dedupe_prefix(event, dedupe_material)
        if self.store.has_event_key_prefix(legacy_dedupe_prefix):
            LOG.info("Skipping duplicate event notification for %s", event.pane_id)
            return
        if not self.store.mark_event_seen(dedupe_key):
            LOG.info("Skipping duplicate event notification for %s", event.pane_id)
            return

        destination = await self._destination_for(event.pane_id)
        if destination is None:
            LOG.warning("No Discord destination for Herdr event %s", event.pane_id)
            return

        title = event_title(event)
        body = title
        if output:
            body += "\n" + format_tail(output, max_chars=self.config.max_output_chars)
        mention_prefix = blocked_mention_prefix(self.config) if event.status == "blocked" else ""
        view = self.card_view_factory(event.pane_id) if self.card_view_factory else None
        await destination.send(mention_prefix + body, view=view)
        LOG.info("Posted %s notification for %s", event.status, event.pane_id)

    async def _destination_for(self, pane_id: str) -> discord.abc.Messageable | None:
        binding = self.store.find_binding_for_target(pane_id)
        if binding:
            channel = await self._channel_for_binding(binding)
            if channel:
                return channel
        return None

    async def _channel_for_binding(self, binding: Binding) -> discord.abc.Messageable | None:
        target_id = int(binding.thread_id or binding.channel_id)
        return await self._get_channel(target_id)

    async def _get_channel(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except Exception:
            LOG.exception("Failed to fetch Discord channel %s", channel_id)
            return None
        return fetched if isinstance(fetched, discord.abc.Messageable) else None

    async def _sleep_reconnect(self) -> None:
        try:
            await asyncio.wait_for(
                self._stopped.wait(),
                timeout=self.config.watcher.reconnect_delay_seconds,
            )
        except TimeoutError:
            return


def blocked_mention_prefix(config: AppConfig) -> str:
    """Mention allowed users so blocked notifications push to mobile."""
    if not config.allowed_user_ids:
        return ""
    return " ".join(f"<@{uid}>" for uid in config.allowed_user_ids) + "\n"


def resolve_socket_path(config: AppConfig) -> str:
    if config.herdr_socket_path:
        return config.herdr_socket_path
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    if os.environ.get("HERDR_SESSION"):
        return str(Path.home() / ".config" / "herdr" / "sessions" / os.environ["HERDR_SESSION"] / "herdr.sock")
    return str(Path.home() / ".config" / "herdr" / "herdr.sock")


def build_agent_status_subscriptions(client: HerdrClient, config: AppConfig) -> list[dict[str, str]]:
    statuses = tuple(status.casefold() for status in config.watcher.statuses)
    subscriptions = []
    for target in client.list_targets():
        if not target.target:
            continue
        for status in statuses:
            subscriptions.append(
                {
                    "type": "pane.agent_status_changed",
                    "pane_id": target.target,
                    "agent_status": status,
                }
            )
    if not subscriptions:
        raise RuntimeError("No Herdr targets available for event subscriptions")
    return subscriptions


def should_resubscribe(subscribed_at: float, config: AppConfig) -> bool:
    interval = config.watcher.resubscribe_interval_seconds
    return interval > 0 and (time.monotonic() - subscribed_at) >= interval


def parse_agent_status_event(payload: Any) -> AgentStatusEvent | None:
    if not isinstance(payload, dict):
        return None
    event = _event_payload(payload)
    details = _nested_dict(event, "payload") or _nested_dict(event, "data") or event
    event_type = _first_str(event, "type", "event", "name") or _first_str(payload, "type", "event", "name")
    if event_type != "pane.agent_status_changed":
        return None

    pane_id = (
        _first_str(details, "pane_id", "target", "id")
        or _first_str(event, "pane_id", "target", "id")
        or _first_str(payload, "pane_id", "target", "id")
    )
    pane = _nested_dict(details, "pane") or _nested_dict(event, "pane") or _nested_dict(payload, "pane")
    agent = _nested_dict(details, "agent") or _nested_dict(event, "agent") or _nested_dict(payload, "agent")
    pane_id = pane_id or _first_str(pane, "pane_id", "id")
    status = (
        _first_str(details, "status", "agent_status", "new_status")
        or _first_str(event, "status", "agent_status", "new_status")
        or _first_str(pane, "agent_status", "status")
        or _first_str(agent, "agent_status", "status")
    )
    if not pane_id or not status:
        return None
    return AgentStatusEvent(
        pane_id=pane_id,
        status=status.casefold(),
        previous_status=_first_str(details, "previous_status", "old_status")
        or _first_str(event, "previous_status", "old_status"),
        agent_name=_first_str(details, "agent_name", "agent")
        or _first_str(event, "agent_name", "agent")
        or _first_str(agent, "agent_name", "name"),
        raw=payload,
    )


def event_state_marker(client: HerdrClient, event: AgentStatusEvent) -> str:
    """Stable marker for status-only notifications without reading pane output."""
    try:
        target = client.resolve_target(event.pane_id)
        sequence = (target.raw or {}).get("state_change_seq")
        if sequence is not None:
            return f"state-change:{sequence}"
    except Exception:
        pass
    return json.dumps(event.raw or {}, sort_keys=True, default=str)


def event_title(event: AgentStatusEvent) -> str:
    agent = f" {event.agent_name}" if event.agent_name else ""
    if event.previous_status:
        return f"`{event.pane_id}`{agent}: {event.previous_status} -> {event.status}"
    return f"`{event.pane_id}`{agent}: {event.status}"


def event_dedupe_key(event: AgentStatusEvent, output: str) -> str:
    return event_legacy_dedupe_prefix(event, output)


def event_legacy_dedupe_prefix(event: AgentStatusEvent, output: str) -> str:
    digest = hashlib.sha256(output.encode(errors="replace")).hexdigest()[:16]
    return f"{event.pane_id}:{event.status}:{digest}"


def _event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("event", "params", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _nested_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _first_str(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None
