from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import discord

from .config import AppConfig, effective_voice_input
from .formatter import truncate
from .models import TransportAuditEntry
from .store import Store
from .voice_input import (
    VoiceInputProblem,
    VoiceInputProcessor,
    VoiceMedia,
    transcription_detail,
)
from .voice_transcription import VoiceTranscriber

LOG = logging.getLogger(__name__)


class DiscordVoiceHandler:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        transcriber: VoiceTranscriber | None = None,
    ):
        self.voice_config = effective_voice_input(config)
        self.store = store
        self.processor = VoiceInputProcessor(
            config=self.voice_config,
            transcriber=transcriber,
        )

    async def transcribe(self, message: discord.Message) -> str | None:
        attachments = tuple(getattr(message, "attachments", ()) or ())
        if len(attachments) != 1:
            await self._fail(
                message,
                problem=VoiceInputProblem(
                    outcome="failed",
                    detail=(
                        "Discord voice message did not contain exactly one attachment"
                    ),
                    public_message=(
                        "The voice message attachment is invalid. Nothing was sent."
                    ),
                ),
            )
            return None
        attachment = attachments[0]

        async def download(path: Path, _max_bytes: int) -> int:
            return await attachment.save(path)

        media = VoiceMedia(
            download=download,
            duration_seconds=getattr(attachment, "duration", None),
            advertised_size=getattr(attachment, "size", None),
            mime_type=getattr(attachment, "content_type", None),
            filename=getattr(attachment, "filename", None),
        )
        try:
            transcript = await self.processor.process(
                media,
                temporary_prefix="discord-",
            )
        except VoiceInputProblem as problem:
            LOG.warning("Discord voice transcription failed: %s", problem.detail)
            await self._fail(message, problem=problem)
            return None

        self._audit(
            message,
            outcome="succeeded",
            detail=transcription_detail(
                self.processor.transcriber,
                transcript,
            ),
            payload=transcript.text,
        )
        await _reply(
            message,
            "Transcript:\n" + truncate(transcript.text, max_chars=500),
        )
        return transcript.text

    async def _fail(
        self,
        message: discord.Message,
        *,
        problem: VoiceInputProblem,
    ) -> None:
        self._audit(
            message,
            outcome=problem.outcome,
            detail=problem.detail,
        )
        await _reply(message, problem.public_message)

    def _audit(
        self,
        message: discord.Message,
        *,
        outcome: str,
        detail: str,
        payload: str = "",
    ) -> None:
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        self.store.add_transport_audit(
            TransportAuditEntry(
                transport="discord",
                actor_id=str(getattr(author, "id", 0)),
                container_id=str(getattr(guild, "id", 0)),
                conversation_id=str(getattr(channel, "id", 0)),
                action="transcribe_voice",
                target_key=None,
                payload_preview=truncate(payload, max_chars=200),
                outcome=outcome,
                detail=truncate(detail, max_chars=500),
            )
        )


def is_discord_voice_message(message: Any) -> bool:
    flags = getattr(message, "flags", None)
    return bool(getattr(flags, "voice", False))


async def _reply(message: discord.Message, text: str) -> None:
    await message.reply(
        text,
        allowed_mentions=discord.AllowedMentions.none(),
        mention_author=False,
    )
