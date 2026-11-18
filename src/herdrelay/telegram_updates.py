from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import TelegramConfig


@dataclass(frozen=True, slots=True)
class TelegramInboundMessage:
    update_id: int
    message_id: int
    user_id: int
    chat_id: int
    message_thread_id: int | None
    chat_type: str
    is_forum: bool
    text: str

    @property
    def command(self) -> str | None:
        stripped = self.text.strip()
        first = stripped.split(maxsplit=1)[0] if stripped else ""
        if not first.startswith("/") or first.startswith("//"):
            return None
        return first[1:].split("@", 1)[0].casefold()

    @property
    def prompt_text(self) -> str:
        stripped = self.text.strip()
        return stripped[1:] if stripped.startswith("//") else stripped


@dataclass(frozen=True, slots=True)
class TelegramInboundCallback:
    update_id: int
    callback_query_id: str
    user_id: int
    chat_id: int
    message_thread_id: int | None
    chat_type: str
    is_forum: bool
    data: str


class TelegramUpdatePolicy:
    def __init__(self, config: TelegramConfig):
        self.config = config

    def authorized(
        self, message: TelegramInboundMessage | TelegramInboundCallback
    ) -> bool:
        if message.chat_id not in self.config.allowed_chat_ids:
            return False
        if message.user_id not in self.config.allowed_user_ids:
            return False
        if self.config.mode == "forum":
            return message.chat_type == "supergroup" and message.is_forum
        return False


def parse_inbound_callback(value: Any) -> TelegramInboundCallback | None:
    if not isinstance(value, dict):
        raise ValueError("Telegram update must be an object")
    update_id = _integer(value, "update_id")
    callback = value.get("callback_query")
    if callback is None:
        return None
    if not isinstance(callback, dict):
        raise ValueError("Telegram callback_query must be an object")
    callback_id = callback.get("id")
    data = callback.get("data")
    if not isinstance(callback_id, str) or not callback_id:
        raise ValueError("Telegram callback query ID must be a string")
    if not isinstance(data, str) or not data:
        return None
    sender = callback.get("from")
    if not isinstance(sender, dict) or sender.get("is_bot") is True:
        return None
    message = callback.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        raise ValueError("Telegram callback chat must be an object")
    thread_id = message.get("message_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, int) or isinstance(thread_id, bool)
    ):
        raise ValueError("Telegram callback message_thread_id must be an integer")
    chat_type = chat.get("type")
    is_forum = chat.get("is_forum", False)
    if not isinstance(chat_type, str) or not chat_type:
        raise ValueError("Telegram callback chat type must be a string")
    if not isinstance(is_forum, bool):
        raise ValueError("Telegram callback chat is_forum must be a boolean")
    return TelegramInboundCallback(
        update_id=update_id,
        callback_query_id=callback_id,
        user_id=_integer(sender, "id", label="user ID"),
        chat_id=_integer(chat, "id", label="chat ID"),
        message_thread_id=thread_id,
        chat_type=chat_type,
        is_forum=is_forum,
        data=data,
    )


def parse_inbound_message(value: Any) -> TelegramInboundMessage | None:
    if not isinstance(value, dict):
        raise ValueError("Telegram update must be an object")
    update_id = _integer(value, "update_id")
    message = value.get("message")
    if message is None:
        return None
    if not isinstance(message, dict):
        raise ValueError("Telegram message must be an object")
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    if sender.get("is_bot") is True:
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        raise ValueError("Telegram message chat must be an object")
    thread_id = message.get("message_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, int) or isinstance(thread_id, bool)
    ):
        raise ValueError("Telegram message_thread_id must be an integer")
    chat_type = chat.get("type")
    if not isinstance(chat_type, str) or not chat_type:
        raise ValueError("Telegram chat type must be a string")
    is_forum = chat.get("is_forum", False)
    if not isinstance(is_forum, bool):
        raise ValueError("Telegram chat is_forum must be a boolean")
    return TelegramInboundMessage(
        update_id=update_id,
        message_id=_integer(message, "message_id"),
        user_id=_integer(sender, "id", label="user ID"),
        chat_id=_integer(chat, "id", label="chat ID"),
        message_thread_id=thread_id,
        chat_type=chat_type,
        is_forum=is_forum,
        text=text,
    )


def _integer(
    item: dict[str, Any], key: str, *, label: str | None = None
) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Telegram {label or key} must be an integer")
    return value
