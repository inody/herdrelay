from __future__ import annotations

import asyncio
import logging
from time import monotonic

from .config import AppConfig
from .domain import BackendKind, TargetKey
from .formatter import target_alias
from .hc_client import HcClient
from .hc_events import HcEventStream, HcStatusTransition, HcTerminalState
from .hc_protocol import HcEventNotification
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_topics import TelegramTopicManager

LOG = logging.getLogger(__name__)


class TelegramStatusRelay:
    """Create target topics and relay runtime status transitions."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
        api: TelegramBotApi,
        stopped: asyncio.Event,
        topics: TelegramTopicManager,
    ):
        self.config = config
        self.telegram = config.telegram
        self.store = store
        self.client = client
        self.core_client = client if isinstance(client, HcClient) else None
        self.api = api
        self.stopped = stopped
        self.topics = topics
        self._legacy_statuses: dict[str, str | None] = {}
        self._legacy_live_targets: dict[str, TargetKey] = {}
        self._legacy_missing_since: dict[str, float] = {}
        self._legacy_topics_restored = False

    async def run_forever(self) -> None:
        while not self.stopped.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Telegram runtime status relay failed; reconnecting")
                await self._sleep_reconnect()

    async def run_once(self) -> None:
        if self.core_client is not None:
            await self._core_once()
            return
        await self.legacy_once()
        try:
            await asyncio.wait_for(
                self.stopped.wait(),
                timeout=self.telegram.status_refresh_seconds,
            )
        except TimeoutError:
            pass

    async def _core_once(self) -> None:
        assert self.core_client is not None
        subscription = await HcEventStream(self.config.runtime.core).connect()
        self.core_client.observe_core_identity(
            subscription.hello.core.id, subscription.hello.core.name
        )
        core_id = subscription.hello.core.id
        state = HcTerminalState(subscription.snapshot)
        await self.reconcile_core_topics(core_id, state)
        await self.topics.cleanup_expired_closed()

        stop_task = asyncio.create_task(self.stopped.wait())
        notification_task: asyncio.Task | None = None
        try:
            while not self.stopped.is_set():
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
                    await self.deliver_core_status(core_id, state, transition)
                if isinstance(notification, HcEventNotification):
                    await self.handle_core_lifecycle(core_id, state, notification)
                await self.topics.cleanup_expired_closed()
        finally:
            if notification_task is not None:
                notification_task.cancel()
                await asyncio.gather(notification_task, return_exceptions=True)
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await subscription.close()

    async def legacy_once(self) -> None:
        panes = await asyncio.to_thread(self.client.list_panes)
        self._restore_open_legacy_topics()
        live_targets = {
            key.address: key
            for target in panes
            for key in (self.client.target_key(target.target),)
        }
        detected: set[str] = set()
        for target in panes:
            key = self.client.target_key(target.target)
            route_target = key.to_route_value()
            if not target.agent_name:
                self._legacy_statuses.pop(route_target, None)
                continue
            detected.add(route_target)
            topic = await self.topics.ensure(key, target)
            current = target.status.casefold() if target.status else None
            previous = self._legacy_statuses.get(route_target)
            self._legacy_statuses[route_target] = current
            if (
                previous is not None
                and current != previous
                and current in self.telegram.statuses
            ):
                icon = "🔴" if current == "blocked" else "✅"
                await self.api.send_text(
                    chat_id=int(topic.chat_id),
                    message_thread_id=int(topic.thread_id),
                    text=(
                        f"{icon} {target_alias(target)}/{target.agent_name}"
                        f"\n{current}"
                    ),
                )
        now = monotonic()
        for address, key in live_targets.items():
            self._legacy_live_targets[address] = key
            self._legacy_missing_since.pop(address, None)
        for address in tuple(
            self._legacy_live_targets.keys() - live_targets.keys()
        ):
            missing_since = self._legacy_missing_since.setdefault(address, now)
            if (
                now - missing_since
                < self.telegram.target_absence_grace_seconds
            ):
                continue
            stale_key = self._legacy_live_targets.pop(address)
            self._legacy_missing_since.pop(address, None)
            await self.topics.close(stale_key)
            self._legacy_statuses.pop(stale_key.to_route_value(), None)
        for route_target in tuple(self._legacy_statuses):
            if route_target not in detected:
                self._legacy_statuses.pop(route_target, None)
        await self.topics.cleanup_expired_closed()

    def _restore_open_legacy_topics(self) -> None:
        if self._legacy_topics_restored:
            return
        try:
            backend = BackendKind(self.config.runtime.provider)
        except ValueError:
            self._legacy_topics_restored = True
            return
        chat_id = self.telegram.forum_chat_id
        for topic in self.store.list_telegram_topics():
            if topic.closed or chat_id is None or int(topic.chat_id) != chat_id:
                continue
            try:
                key = TargetKey.from_route_value(topic.route_target)
            except ValueError:
                LOG.warning(
                    "Ignoring invalid persisted Telegram target route: %s",
                    topic.route_target,
                )
                continue
            if key.backend is backend:
                self._legacy_live_targets[key.address] = key
        self._legacy_topics_restored = True

    async def reconcile_core_topics(
        self, core_id: str, state: HcTerminalState
    ) -> None:
        assert self.core_client is not None
        for terminal in state.terminals.values():
            if not self.core_client.address_allowed(terminal.address):
                continue
            target = self.core_client.project_terminal(terminal)
            if target.agent_name:
                await self.ensure_core_topic(core_id, terminal.address, target)

    async def deliver_core_status(
        self,
        core_id: str,
        state: HcTerminalState,
        transition: HcStatusTransition,
    ) -> None:
        if transition.current_state.casefold() not in self.telegram.statuses:
            return
        assert self.core_client is not None
        if not self.core_client.address_allowed(transition.terminal):
            return
        terminal = state.terminals.get(transition.terminal)
        if terminal is None:
            return
        target = self.core_client.project_terminal(terminal)
        if not target.agent_name:
            return
        topic = await self.ensure_core_topic(core_id, transition.terminal, target)
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

    async def handle_core_lifecycle(
        self,
        core_id: str,
        state: HcTerminalState,
        notification: HcEventNotification,
    ) -> None:
        address = notification.terminal
        if not address or self.core_client is None:
            return
        if notification.kind in {"created", "restarted"}:
            terminal = state.terminals.get(address)
            if terminal and self.core_client.address_allowed(address):
                target = self.core_client.project_terminal(terminal)
                if target.agent_name:
                    await self.ensure_core_topic(core_id, address, target)
        elif notification.kind in {"finished", "removed"}:
            await self.topics.close(
                TargetKey.core(controller_core_id=core_id, address=address)
            )

    async def ensure_core_topic(self, core_id: str, address: str, target):
        return await self.topics.ensure(
            TargetKey.core(controller_core_id=core_id, address=address), target
        )

    async def _sleep_reconnect(self) -> None:
        try:
            await asyncio.wait_for(
                self.stopped.wait(),
                timeout=self.telegram.reconnect_delay_seconds,
            )
        except TimeoutError:
            pass
