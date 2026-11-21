from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from .config import AppConfig
from .domain import TargetKey
from .formatter import target_route_name
from .models import HerdrTarget, TelegramTopicRoute
from .store import Store
from .telegram_api import TelegramApiError, TelegramBotApi

TOPIC_NAME_LIMIT = 128
TOPIC_CREATION_COOLDOWN_SECONDS = 300
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
        self._cleanup_retry_after: dict[int, float] = {}
        self._creation_retry_after: dict[str, float] = {}
        self._creation_locks: dict[str, asyncio.Lock] = {}

    async def ensure(
        self, key: TargetKey, target: HerdrTarget
    ) -> TelegramTopicRoute:
        chat_id = self.telegram.forum_chat_id
        if chat_id is None:
            raise RuntimeError("Telegram forum_chat_id is not configured")
        name = target_route_name(key, target)[:TOPIC_NAME_LIMIT]
        topic = self.store.get_telegram_topic_for_target(key)
        if topic is None or int(topic.chat_id) != chat_id:
            topic = await self._create_topic_safely(
                key=key,
                chat_id=chat_id,
                name=name,
            )
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
            LOG.info(
                "Reopened Telegram topic %s for target %s",
                topic.thread_id,
                key.address,
            )
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

    async def _create_topic_safely(
        self,
        *,
        key: TargetKey,
        chat_id: int,
        name: str,
    ) -> TelegramTopicRoute:
        identity = f"{key.backend.value}:{key.address}"
        lock = self._creation_locks.setdefault(identity, asyncio.Lock())
        async with lock:
            existing = self.store.get_telegram_topic_for_target(key)
            if existing is not None and int(existing.chat_id) == chat_id:
                self._creation_retry_after.pop(identity, None)
                return existing
            retry_after = self._creation_retry_after.get(identity, 0)
            if retry_after > time.monotonic():
                raise RuntimeError(
                    f"Telegram topic creation is cooling down for {key.address}"
                )
            thread_id: int | None = None
            try:
                result = await self.api.create_forum_topic(
                    chat_id=chat_id,
                    name=name,
                )
                value = result.get("message_thread_id")
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TelegramApiError(
                        "Telegram createForumTopic did not return message_thread_id"
                    )
                thread_id = value
                self.store.upsert_telegram_topic_for_target(
                    target=key,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    name=name,
                )
                topic = self.store.get_telegram_topic_for_target(key)
                if topic is None:
                    raise RuntimeError("Telegram topic route was not persisted")
            except Exception:
                self._creation_retry_after[identity] = (
                    time.monotonic() + TOPIC_CREATION_COOLDOWN_SECONDS
                )
                if thread_id is not None:
                    try:
                        await self.api.delete_forum_topic(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                        )
                    except Exception:
                        LOG.exception(
                            "Could not delete Telegram topic %s after route failure",
                            thread_id,
                        )
                    else:
                        LOG.warning(
                            "Deleted newly created Telegram topic %s after route failure",
                            thread_id,
                        )
                existing = self.store.get_telegram_topic_for_target(key)
                if existing is not None and int(existing.chat_id) == chat_id:
                    self._creation_retry_after.pop(identity, None)
                    LOG.warning(
                        "Discarded newly created Telegram topic %s; reusing topic %s",
                        thread_id,
                        existing.thread_id,
                    )
                    return existing
                raise
            self._creation_retry_after.pop(identity, None)
            return topic

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
        LOG.info(
            "Closed Telegram topic %s for target %s",
            topic.thread_id,
            key.address,
        )

    async def cleanup_expired_closed(
        self, *, now: datetime | None = None
    ) -> tuple[int, ...]:
        retention_minutes = self.telegram.closed_topic_retention_minutes
        retention_days = self.telegram.closed_topic_retention_days
        chat_id = self.telegram.forum_chat_id
        if retention_minutes is None and retention_days is None:
            return ()
        if chat_id is None:
            return ()
        retention = (
            timedelta(minutes=retention_minutes)
            if retention_minutes is not None
            else timedelta(days=retention_days or 0)
        )
        current_time = now or datetime.now(UTC)
        cutoff = current_time - retention
        deleted: list[int] = []
        monotonic_now = time.monotonic()
        for topic in self.store.list_telegram_topics():
            thread_id = int(topic.thread_id)
            if (
                not topic.closed
                or int(topic.chat_id) != chat_id
                or datetime.fromisoformat(topic.updated_at) > cutoff
                or self._cleanup_retry_after.get(thread_id, 0) > monotonic_now
            ):
                continue
            try:
                await self.api.delete_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                )
            except TelegramApiError as exc:
                if not _topic_missing(exc):
                    self._cleanup_retry_after[thread_id] = monotonic_now + 3600
                    LOG.warning(
                        "Could not delete expired Telegram topic %s; retrying later: %s",
                        thread_id,
                        exc,
                    )
                    continue
                LOG.info(
                    "Telegram topic %s was already absent during retention cleanup",
                    thread_id,
                )
            self._cleanup_retry_after.pop(thread_id, None)
            self.store.delete_telegram_topic_for_target(topic.route_target)
            deleted.append(thread_id)
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
