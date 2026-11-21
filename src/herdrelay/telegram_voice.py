from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
import tempfile
from typing import Protocol

from .config import AppConfig, effective_voice_input
from .formatter import truncate
from .models import TransportAuditEntry
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler
from .telegram_updates import TelegramInboundMessage
from .voice_transcription import VoiceTranscriber, build_voice_transcriber

LOG = logging.getLogger(__name__)


class TelegramGeneralHandler(Protocol):
    mutations: TelegramMutationHandler

    def accepts(self, message: TelegramInboundMessage) -> bool: ...

    async def handle(
        self, message: TelegramInboundMessage, *, event_key: str
    ) -> bool: ...


class TelegramVoiceHandler:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        api: TelegramBotApi,
        router: TelegramGeneralHandler,
        transcriber: VoiceTranscriber | None = None,
    ):
        self.config = config
        self.voice_config = effective_voice_input(config)
        self.store = store
        self.api = api
        self.router = router
        self.transcriber = transcriber or build_voice_transcriber(
            self.voice_config.transcription
        )

    async def handle(self, message: TelegramInboundMessage, *, event_key: str) -> bool:
        voice = message.voice
        if voice is None:
            return False
        is_general = self.router.accepts(message)
        route = None
        if not is_general:
            if self.voice_config.scope != "general-and-bound":
                self._audit(
                    message,
                    outcome="rejected",
                    detail="voice input is limited to General",
                )
                await self._feedback(
                    message,
                    "Voice input is currently available only in General. Nothing was sent.",
                )
                return False
            if message.message_thread_id is None:
                return False
            route = self.store.get_telegram_topic_by_destination(
                chat_id=message.chat_id,
                thread_id=message.message_thread_id,
            )
            if route is None or route.closed:
                self._audit(
                    message,
                    outcome="rejected",
                    detail="bound topic route is unavailable",
                )
                await self._feedback(
                    message,
                    "This topic is not connected to an available Agent.",
                )
                return False
        if voice.duration_seconds > self.voice_config.max_duration_seconds:
            detail = (
                f"duration {voice.duration_seconds}s exceeds "
                f"{self.voice_config.max_duration_seconds}s"
            )
            self._audit(message, outcome="rejected", detail=detail)
            await self._feedback(
                message,
                "The voice message is too long. Nothing was sent.",
            )
            return False
        if (
            voice.file_size is not None
            and voice.file_size > self.voice_config.max_file_bytes
        ):
            detail = (
                f"file size {voice.file_size} exceeds "
                f"{self.voice_config.max_file_bytes}"
            )
            self._audit(message, outcome="rejected", detail=detail)
            await self._feedback(
                message,
                "The voice file is too large. Nothing was sent.",
            )
            return False

        cache_dir = Path("~/.cache/herdrelay/voice-input").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.chmod(0o700)
        suffix = _audio_suffix(voice.mime_type)
        try:
            with tempfile.TemporaryDirectory(
                prefix="telegram-", dir=cache_dir
            ) as temporary:
                audio_path = Path(temporary) / f"voice{suffix}"
                file_info = await self.api.download_file(
                    file_id=voice.file_id,
                    destination=audio_path,
                    max_bytes=self.voice_config.max_file_bytes,
                )
                if (
                    file_info is not None
                    and file_info.file_unique_id is not None
                    and file_info.file_unique_id != voice.file_unique_id
                ):
                    raise ValueError("Telegram voice file identity changed")
                transcript = await self.transcriber.transcribe(audio_path)
        except Exception as exc:
            LOG.warning("Telegram voice transcription failed: %s", exc)
            self._audit(message, outcome="failed", detail=str(exc))
            await self._feedback(
                message,
                "The voice message could not be transcribed. Nothing was sent.",
            )
            return False

        self._audit(
            message,
            outcome="succeeded",
            detail=(
                f"provider={self.transcriber.provider_name} "
                f"model={self.transcriber.model_name or '-'} "
                f"language={transcript.language or '-'}"
            ),
            payload=transcript.text,
        )
        await self._feedback(
            message,
            "Transcript:\n" + truncate(transcript.text, max_chars=500),
        )
        routed_message = replace(
            message,
            text=transcript.text,
            voice=None,
            source_kind="voice",
        )
        if is_general:
            return await self.router.handle(routed_message, event_key=event_key)
        return await self.router.mutations.send_prompt(
            routed_message,
            event_key=event_key,
            route=route,
            text=transcript.text,
        )

    def _audit(
        self,
        message: TelegramInboundMessage,
        *,
        outcome: str,
        detail: str,
        payload: str = "",
    ) -> None:
        self.store.add_transport_audit(
            TransportAuditEntry(
                transport="telegram",
                actor_id=str(message.user_id),
                container_id=str(message.chat_id),
                conversation_id=(
                    str(message.message_thread_id)
                    if message.message_thread_id is not None
                    else None
                ),
                action="transcribe_voice",
                target_key=None,
                payload_preview=truncate(payload, max_chars=200),
                outcome=outcome,
                detail=truncate(detail, max_chars=500),
            )
        )

    async def _feedback(self, message: TelegramInboundMessage, text: str) -> None:
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )


def _audio_suffix(mime_type: str | None) -> str:
    return {
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
    }.get((mime_type or "").casefold(), ".ogg")
