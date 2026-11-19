from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import AppConfig
from .domain import TargetKey
from .formatter import target_address_label, target_alias
from .models import HerdrTarget, TelegramTopicRoute
from .store import Store
from .telegram_api import TelegramApiError, TelegramBotApi

TOPIC_NAME_LIMIT = 128
LOG = logging.getLogger(__name__)


class TelegramTopicManager:
    """Persist one Telegram forum topic for each normalized runtime target."""

    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        api: TelegramBotApi,
    ):
        self.config = config
        self.telegram = config.telegram
        self.store = store
        self.api = api

    async def ensure(
        self, key: TargetKey, target: HerdrTarget
    ) -> TelegramTopicRoute:
        chat_id = self.telegram.forum_chat_id
        if chat_id is None:
            raise RuntimeError("Telegram forum_chat_id is not configured")
        name = (
            f"{target_alias(target)}/{target.agent_name}"
            f" · {target_address_label(key.address)}"
        )[:TOPIC_NAME_LIMIT]
        topic = self.store.get_telegram_topic_for_target(key)
        if topic is None or int(topic.chat_id) != chat_id:
            result = await self.api.create_forum_topic(chat_id=chat_id, name=name)
            thread_id = result.get("message_thread_id")
            if not isinstance(thread_id, int) or isinstance(thread_id, bool):
                raise TelegramApiError(
                    "Telegram createForumTopic did not return message_thread_id"
                )
            self.store.upsert_telegram_topic_for_target(
                target=key,
                chat_id=chat_id,
                thread_id=thread_id,
                name=name,
            )
            topic = self.store.get_telegram_topic_for_target(key)
            assert topic is not None
            return topic
        if topic.route_target != key.to_route_value():
            self.store.upsert_telegram_topic_for_target(
                target=key,
                chat_id=chat_id,
                thread_id=topic.thread_id,
                name=topic.name,
                closed=topic.closed,
            )
            topic = self.store.get_telegram_topic_for_target(key)
            assert topic is not None
        if topic.closed:
            try:
                await self.api.reopen_forum_topic(
                    chat_id=chat_id, message_thread_id=int(topic.thread_id)
                )
            except TelegramApiError as exc:
                if not _topic_not_modified(exc):
                    raise
            self.store.set_telegram_topic_closed_for_target(key, closed=False)
        if topic.name != name:
            await self.api.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=int(topic.thread_id),
                name=name,
            )
            self.store.upsert_telegram_topic_for_target(
                target=key,
                chat_id=chat_id,
                thread_id=topic.thread_id,
                name=name,
                closed=False,
            )
        refreshed = self.store.get_telegram_topic_for_target(key)
        assert refreshed is not None
        return refreshed

    async def close(self, key: TargetKey) -> None:
        topic = self.store.get_telegram_topic_for_target(key)
        if topic is None or topic.closed:
            return
        try:
            await self.api.close_forum_topic(
                chat_id=int(topic.chat_id),
                message_thread_id=int(topic.thread_id),
            )
        except TelegramApiError as exc:
            if not _topic_not_modified(exc):
                raise
        self.store.set_telegram_topic_closed_for_target(key, closed=True)

    async def cleanup_expired_closed(
        self, *, now: datetime | None = None
    ) -> tuple[int, ...]:
        retention_days = self.telegram.closed_topic_retention_days
        chat_id = self.telegram.forum_chat_id
        if retention_days is None or chat_id is None:
            return ()
        current_time = now or datetime.now(UTC)
        cutoff = current_time - timedelta(days=retention_days)
        deleted: list[int] = []
        for topic in self.store.list_telegram_topics():
            if (
                not topic.closed
                or int(topic.chat_id) != chat_id
                or datetime.fromisoformat(topic.updated_at) > cutoff
            ):
                continue
            try:
                await self.api.delete_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=int(topic.thread_id),
                )
            except TelegramApiError as exc:
                if not _topic_missing(exc):
                    raise
                LOG.info(
                    "Telegram topic %s was already absent during retention cleanup",
                    topic.thread_id,
                )
            self.store.delete_telegram_topic_for_target(topic.route_target)
            deleted.append(int(topic.thread_id))
        return tuple(deleted)


def _topic_not_modified(exc: TelegramApiError) -> bool:
    return "topic_not_modified" in str(exc).casefold()


def _topic_missing(exc: TelegramApiError) -> bool:
    detail = str(exc).casefold()
    return any(
        marker in detail
        for marker in (
            "message thread not found",
            "topic_id_invalid",
            "forum topic not found",
            "message to delete not found",
        )
    )
