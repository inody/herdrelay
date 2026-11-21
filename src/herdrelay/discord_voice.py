from __future__ import annotations

import logging
from pathlib import Path
import tempfile
from typing import Any

import discord

from .config import AppConfig, effective_voice_input
from .formatter import truncate
from .models import TransportAuditEntry
from .store import Store
from .voice_transcription import VoiceTranscriber, build_voice_transcriber

LOG = logging.getLogger(__name__)


class DiscordVoiceHandler:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        transcriber: VoiceTranscriber | None = None,
    ):
        self.config = config
        self.voice_config = effective_voice_input(config)
        self.store = store
        self.transcriber = transcriber or build_voice_transcriber(
            self.voice_config.transcription
        )

    async def transcribe(self, message: discord.Message) -> str | None:
        attachments = tuple(getattr(message, "attachments", ()) or ())
        if len(attachments) != 1:
            await self._fail(
                message,
                detail="Discord voice message did not contain exactly one attachment",
                feedback="The voice message attachment is invalid. Nothing was sent.",
            )
            return None
        attachment = attachments[0]
        duration = getattr(attachment, "duration", None)
        if duration is not None and duration > self.voice_config.max_duration_seconds:
            await self._fail(
                message,
                detail=(
                    f"duration {duration}s exceeds "
                    f"{self.voice_config.max_duration_seconds}s"
                ),
                feedback="The voice message is too long. Nothing was sent.",
                outcome="rejected",
            )
            return None
        size = getattr(attachment, "size", None)
        if size is not None and size > self.voice_config.max_file_bytes:
            await self._fail(
                message,
                detail=f"file size {size} exceeds {self.voice_config.max_file_bytes}",
                feedback="The voice file is too large. Nothing was sent.",
                outcome="rejected",
            )
            return None

        cache_dir = Path("~/.cache/herdrelay/voice-input").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.chmod(0o700)
        suffix = _audio_suffix(
            getattr(attachment, "content_type", None),
            getattr(attachment, "filename", None),
        )
        try:
            with tempfile.TemporaryDirectory(prefix="discord-", dir=cache_dir) as temporary:
                audio_path = Path(temporary) / f"voice{suffix}"
                written = await attachment.save(audio_path)
                actual_size = audio_path.stat().st_size
                if (
                    written > self.voice_config.max_file_bytes
                    or actual_size > self.voice_config.max_file_bytes
                ):
                    raise ValueError("downloaded Discord voice file exceeds configured limit")
                transcript = await self.transcriber.transcribe(audio_path)
        except Exception as exc:
            LOG.warning("Discord voice transcription failed: %s", exc)
            await self._fail(
                message,
                detail=str(exc),
                feedback="The voice message could not be transcribed. Nothing was sent.",
            )
            return None

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
        await _reply(
            message,
            "Transcript:\n" + truncate(transcript.text, max_chars=500),
        )
        return transcript.text

    async def _fail(
        self,
        message: discord.Message,
        *,
        detail: str,
        feedback: str,
        outcome: str = "failed",
    ) -> None:
        self._audit(message, outcome=outcome, detail=detail)
        await _reply(message, feedback)

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


def _audio_suffix(content_type: str | None, filename: str | None) -> str:
    by_type = {
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get((content_type or "").split(";", 1)[0].casefold())
    if by_type is not None:
        return by_type
    suffix = Path(filename or "").suffix.casefold()
    return suffix if suffix in {".ogg", ".opus", ".mp3", ".m4a", ".webm", ".wav"} else ".ogg"
