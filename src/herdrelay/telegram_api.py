from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from .formatter import split_text_chunks

TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_TEXT_CHUNK = 4000


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class TelegramFile:
    file_id: str
    file_unique_id: str | None
    file_size: int | None
    file_path: str


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    message_id: int
    chat_id: int
    message_thread_id: int | None
    raw: dict[str, Any]


class TelegramBotApi:
    """Small async Bot API client with no webhook or inbound listener."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        request_timeout_seconds: float = 35,
        session: aiohttp.ClientSession | None = None,
    ):
        if not token.strip():
            raise ValueError("Telegram bot token must not be empty")
        if request_timeout_seconds <= 0:
            raise ValueError("Telegram request timeout must be positive")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> TelegramBotApi:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe", {})

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 25,
        allowed_updates: tuple[str, ...] = ("message", "callback_query"),
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": list(allowed_updates),
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload)
        if not isinstance(result, list) or any(
            not isinstance(update, dict) for update in result
        ):
            raise TelegramApiError("Telegram getUpdates result must be an array")
        return result

    async def get_file(self, file_id: str) -> TelegramFile:
        if not file_id:
            raise ValueError("Telegram file_id must not be empty")
        result = await self.call("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram getFile result must be an object")
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramApiError("Telegram getFile file_path must be a string")
        unique_id = result.get("file_unique_id")
        if unique_id is not None and not isinstance(unique_id, str):
            raise TelegramApiError("Telegram getFile file_unique_id must be a string")
        file_size = result.get("file_size")
        if file_size is not None and (
            not isinstance(file_size, int)
            or isinstance(file_size, bool)
            or file_size < 0
        ):
            raise TelegramApiError(
                "Telegram getFile file_size must be a non-negative integer"
            )
        returned_id = result.get("file_id", file_id)
        if not isinstance(returned_id, str) or not returned_id:
            raise TelegramApiError("Telegram getFile file_id must be a string")
        return TelegramFile(
            file_id=returned_id,
            file_unique_id=unique_id,
            file_size=file_size,
            file_path=file_path,
        )

    async def download_file(
        self,
        *,
        file_id: str,
        destination: str | Path,
        max_bytes: int,
    ) -> TelegramFile:
        if max_bytes <= 0:
            raise ValueError("Telegram download max_bytes must be positive")
        info = await self.get_file(file_id)
        if info.file_size is not None and info.file_size > max_bytes:
            raise TelegramApiError("Telegram file exceeds the configured size limit")
        await self.start()
        assert self._session is not None
        safe_path = quote(info.file_path.lstrip("/"), safe="/")
        url = f"{self._base_url}/file/bot{self._token}/{safe_path}"
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._session.get(url) as response:
                if response.status >= 400:
                    raise TelegramApiError(
                        f"Telegram file download returned HTTP {response.status}"
                    )
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise TelegramApiError(
                                "Telegram file exceeds the configured size limit"
                            )
                        stream.write(chunk)
        except asyncio.TimeoutError as exc:
            path.unlink(missing_ok=True)
            raise TelegramApiError("Telegram file download timed out") from exc
        except (aiohttp.ClientError, OSError):
            path.unlink(missing_ok=True)
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return info

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramMessage:
        if not text:
            raise ValueError("Telegram message text must not be empty")
        if len(text) > TELEGRAM_TEXT_LIMIT:
            raise ValueError(
                f"Telegram message exceeds {TELEGRAM_TEXT_LIMIT} characters"
            )
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": True},
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self.call("sendMessage", payload)
        return _parse_message(result)

    async def send_text(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        max_chars: int = DEFAULT_TEXT_CHUNK,
        disable_notification: bool = False,
    ) -> tuple[TelegramMessage, ...]:
        if not text:
            raise ValueError("Telegram message text must not be empty")
        if not 1 <= max_chars <= TELEGRAM_TEXT_LIMIT:
            raise ValueError(
                f"Telegram chunk size must be between 1 and {TELEGRAM_TEXT_LIMIT}"
            )
        messages = []
        for chunk in split_text_chunks(text, max_chars=max_chars):
            messages.append(
                await self.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    message_thread_id=message_thread_id,
                    disable_notification=disable_notification,
                )
            )
        return tuple(messages)

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text is not None:
            payload["text"] = text
        result = await self.call("answerCallbackQuery", payload)
        return _boolean_result(result, "answerCallbackQuery")

    async def set_message_reaction(
        self,
        *,
        chat_id: int,
        message_id: int,
        emoji: str,
    ) -> bool:
        if not emoji:
            raise ValueError("Telegram reaction emoji must not be empty")
        result = await self.call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )
        return _boolean_result(result, "setMessageReaction")

    async def create_forum_topic(
        self, *, chat_id: int, name: str
    ) -> dict[str, Any]:
        result = await self.call(
            "createForumTopic", {"chat_id": chat_id, "name": name}
        )
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram createForumTopic result must be an object")
        return result

    async def edit_forum_topic(
        self, *, chat_id: int, message_thread_id: int, name: str
    ) -> bool:
        result = await self.call(
            "editForumTopic",
            {
                "chat_id": chat_id,
                "message_thread_id": message_thread_id,
                "name": name,
            },
        )
        return _boolean_result(result, "editForumTopic")

    async def reopen_forum_topic(
        self, *, chat_id: int, message_thread_id: int
    ) -> bool:
        result = await self.call(
            "reopenForumTopic",
            {"chat_id": chat_id, "message_thread_id": message_thread_id},
        )
        return _boolean_result(result, "reopenForumTopic")

    async def close_forum_topic(
        self, *, chat_id: int, message_thread_id: int
    ) -> bool:
        result = await self.call(
            "closeForumTopic",
            {"chat_id": chat_id, "message_thread_id": message_thread_id},
        )
        return _boolean_result(result, "closeForumTopic")

    async def delete_forum_topic(
        self, *, chat_id: int, message_thread_id: int
    ) -> bool:
        result = await self.call(
            "deleteForumTopic",
            {"chat_id": chat_id, "message_thread_id": message_thread_id},
        )
        return _boolean_result(result, "deleteForumTopic")

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        await self.start()
        assert self._session is not None
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            async with self._session.post(url, json=payload) as response:
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise TelegramApiError(
                        f"Telegram {method} returned invalid JSON (HTTP {response.status})"
                    ) from exc
        except asyncio.TimeoutError as exc:
            raise TelegramApiError(f"Telegram {method} timed out") from exc
        except aiohttp.ClientError as exc:
            raise TelegramApiError(f"Telegram {method} request failed: {exc}") from exc

        if not isinstance(body, dict):
            raise TelegramApiError(f"Telegram {method} response must be an object")
        if response.status >= 400 or body.get("ok") is not True:
            parameters = body.get("parameters")
            retry_after = (
                parameters.get("retry_after")
                if isinstance(parameters, dict)
                and isinstance(parameters.get("retry_after"), int)
                else None
            )
            error_code = body.get("error_code")
            raise TelegramApiError(
                str(body.get("description") or f"Telegram {method} failed"),
                error_code=error_code if isinstance(error_code, int) else None,
                retry_after=retry_after,
            )
        return body.get("result")


def _parse_message(value: Any) -> TelegramMessage:
    if not isinstance(value, dict):
        raise TelegramApiError("Telegram sendMessage result must be an object")
    message_id = value.get("message_id")
    chat = value.get("chat")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise TelegramApiError("Telegram message_id must be an integer")
    if not isinstance(chat, dict):
        raise TelegramApiError("Telegram message chat must be an object")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise TelegramApiError("Telegram chat ID must be an integer")
    thread_id = value.get("message_thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, int) or isinstance(thread_id, bool)
    ):
        raise TelegramApiError("Telegram message_thread_id must be an integer")
    return TelegramMessage(
        message_id=message_id,
        chat_id=chat_id,
        message_thread_id=thread_id,
        raw=value,
    )


def _boolean_result(value: Any, method: str) -> bool:
    if value is not True:
        raise TelegramApiError(f"Telegram {method} result must be true")
    return True
