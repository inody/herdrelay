from __future__ import annotations

from .config import AppConfig
from .domain import RuntimeKind
from .output_events import HookOutputEvent
from .store import Store


def discord_delivery_key(event: HookOutputEvent, thread_id: int | str) -> str:
    return f"agent-output:discord:{thread_id}:{event.agent}:{event.event_id}"


def telegram_delivery_key(
    event: HookOutputEvent, *, chat_id: int | str, thread_id: int | str
) -> str:
    return (
        f"agent-output:telegram:{chat_id}:{thread_id}:"
        f"{event.agent}:{event.event_id}"
    )


def legacy_delivery_key(event: HookOutputEvent) -> str:
    return f"agent-output:{event.agent}:{event.event_id}"


def delivery_seen(store: Store, event: HookOutputEvent, key: str) -> bool:
    if store.has_event_key(key):
        return True
    return event.version == 1 and store.has_event_key(legacy_delivery_key(event))


def mark_delivery_seen(store: Store, event: HookOutputEvent, key: str) -> None:
    store.mark_event_seen(key)
    if event.version == 1:
        store.mark_event_seen(legacy_delivery_key(event))


def required_delivery_keys(
    *, config: AppConfig, store: Store, event: HookOutputEvent
) -> tuple[str, ...]:
    keys: list[str] = []
    route_target = event.target.to_route_value()
    discord_thread = store.get_agent_thread(route_target)
    if discord_thread is not None:
        keys.append(discord_delivery_key(event, discord_thread))

    if (
        config.telegram.enabled
        and event.target.runtime is RuntimeKind.HERDR_CORE
        and event.target.controller_core_id is not None
    ):
        route = store.get_telegram_topic(
            core_id=event.target.controller_core_id,
            terminal_address=event.target.address,
        )
        if route is not None and not route.closed:
            keys.append(
                telegram_delivery_key(
                    event,
                    chat_id=route.chat_id,
                    thread_id=route.thread_id,
                )
            )
    return tuple(keys)


def all_required_deliveries_seen(
    *, config: AppConfig, store: Store, event: HookOutputEvent
) -> bool:
    keys = required_delivery_keys(config=config, store=store, event=event)
    return bool(keys) and all(delivery_seen(store, event, key) for key in keys)
