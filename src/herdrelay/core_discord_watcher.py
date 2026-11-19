from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from .config import AppConfig
from .hc_client import HcClient
from .hc_events import (
    HcEventStream,
    HcStatusTransition,
    HcTerminalState,
)
from .discord_watcher import AgentStatusEvent, EventWatcher


class CoreEventWatcher(EventWatcher):
    """Deliver Core status transitions through the existing Discord notifier."""

    def __init__(
        self,
        *,
        bot,
        config: AppConfig,
        store,
        client: HcClient,
        stream_factory: Callable[[], HcEventStream] | None = None,
    ):
        super().__init__(
            bot=bot,
            config=config,
            store=store,
            client=client,
        )
        self.client = client
        self.stream_factory = stream_factory or (
            lambda: HcEventStream(config.runtime.core)
        )

    async def _run_once(self) -> None:
        subscription = await self.stream_factory().connect()
        self.client.observe_core_identity(
            subscription.hello.core.id, subscription.hello.core.name
        )
        state = HcTerminalState(subscription.snapshot)
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
                for event in core_status_events(
                    self.client,
                    state,
                    transitions,
                    statuses=self.config.watcher.statuses,
                ):
                    await self._notify(event)
        finally:
            if notification_task is not None:
                notification_task.cancel()
                await asyncio.gather(notification_task, return_exceptions=True)
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await subscription.close()


def core_status_events(
    client: HcClient,
    state: HcTerminalState,
    transitions: Iterable[HcStatusTransition],
    *,
    statuses: Iterable[str],
) -> tuple[AgentStatusEvent, ...]:
    desired = {status.casefold() for status in statuses}
    events = []
    for transition in transitions:
        current_state = transition.current_state.casefold()
        if current_state not in desired:
            continue
        terminal = state.terminals.get(transition.terminal)
        if terminal is None or not client.address_allowed(transition.terminal):
            continue
        projected = client.project_terminal(terminal)
        if not projected.agent_name:
            continue
        events.append(
            AgentStatusEvent(
                pane_id=transition.terminal,
                status=current_state,
                previous_status=(
                    transition.previous_state.casefold()
                    if transition.previous_state
                    else None
                ),
                agent_name=projected.agent_name,
                raw={
                    "runtime": "herdr-core",
                    "state_change_seq": state.seq,
                    "turn": terminal.turn,
                    "status_ms": terminal.status_ms,
                    "resynchronized": transition.resynchronized,
                },
            )
        )
    return tuple(events)
