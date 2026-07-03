from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig


@dataclass(frozen=True)
class DiscordLocation:
    guild_id: int | None
    channel_id: int
    thread_id: int | None = None


class SecurityError(ValueError):
    pass


class SecurityPolicy:
    def __init__(self, config: AppConfig):
        self.config = config

    def ensure_read_allowed(self, location: DiscordLocation) -> None:
        if self.config.allowed_guild_ids and location.guild_id not in self.config.allowed_guild_ids:
            raise SecurityError("This Discord server is not allowed.")
        if self.config.allowed_channel_ids:
            ids = {location.channel_id}
            if location.thread_id is not None:
                ids.add(location.thread_id)
            if not ids.intersection(self.config.allowed_channel_ids):
                raise SecurityError("This Discord channel is not allowed.")

    def ensure_send_allowed(self, user_id: int, location: DiscordLocation, message: str) -> None:
        self.ensure_read_allowed(location)
        if not self.config.enable_send:
            raise SecurityError("Send is disabled in config.")
        self._ensure_write_user(user_id)
        if len(message) > self.config.max_message_chars:
            raise SecurityError("Message is too long.")
        lowered = message.casefold()
        for blocked in self.config.dangerous_text_blocklist:
            if blocked.casefold() in lowered:
                raise SecurityError("Message contains blocked text.")

    def ensure_approve_allowed(self, user_id: int, location: DiscordLocation) -> None:
        self.ensure_read_allowed(location)
        self.ensure_approve_user_allowed(user_id)

    def ensure_approve_user_allowed(self, user_id: int) -> None:
        if not self.config.enable_approve:
            raise SecurityError("Approve is disabled in config.")
        self._ensure_write_user(user_id)

    def _ensure_write_user(self, user_id: int) -> None:
        if not self.config.allowed_user_ids:
            raise SecurityError("No write users are configured.")
        if user_id not in self.config.allowed_user_ids:
            raise SecurityError("This Discord user is not allowed to write.")
