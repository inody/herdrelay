from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from .config import AppConfig, effective_voice_input
from .formatter import truncate
from .models import TransportAuditEntry
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler
from .telegram_updates import TelegramInboundMessage
from .voice_input import (
    VoiceInputProblem,
    VoiceInputProcessor,
    VoiceMedia,
    transcription_detail,
)
from .voice_transcription import VoiceTranscriber

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
        self.processor = VoiceInputProcessor(
            config=self.voice_config,
            transcriber=transcriber,
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
        async def download(path: Path, max_bytes: int) -> int | None:
            file_info = await self.api.download_file(
                file_id=voice.file_id,
                destination=path,
                max_bytes=max_bytes,
            )
            if (
                file_info is not None
                and file_info.file_unique_id is not None
                and file_info.file_unique_id != voice.file_unique_id
            ):
                raise ValueError("Telegram voice file identity changed")
            return path.stat().st_size

        media = VoiceMedia(
            download=download,
            duration_seconds=voice.duration_seconds,
            advertised_size=voice.file_size,
            mime_type=voice.mime_type,
        )
        try:
            transcript = await self.processor.process(
                media,
                temporary_prefix="telegram-",
            )
        except VoiceInputProblem as problem:
            LOG.warning("Telegram voice transcription failed: %s", problem.detail)
            self._audit(
                message,
                outcome=problem.outcome,
                detail=problem.detail,
            )
            await self._feedback(message, problem.public_message)
            return False

        self._audit(
            message,
            outcome="succeeded",
            detail=transcription_detail(
                self.processor.transcriber,
                transcript,
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
