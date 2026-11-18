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
        first = self.text.strip().split(maxsplit=1)[0] if self.text.strip() else ""
        if not first.startswith("/"):
            return None
        return first[1:].split("@", 1)[0].casefold()


class TelegramUpdatePolicy:
    def __init__(self, config: TelegramConfig):
        self.config = config

    def authorized(self, message: TelegramInboundMessage) -> bool:
        if message.chat_id not in self.config.allowed_chat_ids:
            return False
        if message.user_id not in self.config.allowed_user_ids:
            return False
        if self.config.mode == "forum":
            return message.chat_type == "supergroup" and message.is_forum
        return False


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
