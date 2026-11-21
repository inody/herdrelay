from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import tempfile

from .config import VoiceInputConfig
from .voice_transcription import (
    Transcript,
    VoiceTranscriber,
    build_voice_transcriber,
)

VoiceDownloader = Callable[[Path, int], Awaitable[int | None]]


@dataclass(frozen=True, slots=True)
class VoiceMedia:
    download: VoiceDownloader
    duration_seconds: float | None = None
    advertised_size: int | None = None
    mime_type: str | None = None
    filename: str | None = None


class VoiceInputProblem(RuntimeError):
    def __init__(
        self,
        *,
        outcome: str,
        detail: str,
        public_message: str,
    ):
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.public_message = public_message


class VoiceInputProcessor:
    """Transport-neutral bounded download and transcription lifecycle."""

    def __init__(
        self,
        *,
        config: VoiceInputConfig,
        transcriber: VoiceTranscriber | None = None,
    ):
        self.config = config
        self.transcriber = transcriber or build_voice_transcriber(
            config.transcription
        )

    async def process(self, media: VoiceMedia, *, temporary_prefix: str) -> Transcript:
        self._validate_metadata(media)
        cache_dir = Path("~/.cache/herdrelay/voice-input").expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir.chmod(0o700)
        suffix = audio_suffix(media.mime_type, media.filename)
        try:
            with tempfile.TemporaryDirectory(
                prefix=temporary_prefix,
                dir=cache_dir,
            ) as temporary:
                audio_path = Path(temporary) / f"voice{suffix}"
                written = await media.download(
                    audio_path,
                    self.config.max_file_bytes,
                )
                actual_size = audio_path.stat().st_size
                if (
                    (written is not None and written > self.config.max_file_bytes)
                    or actual_size > self.config.max_file_bytes
                ):
                    raise VoiceInputProblem(
                        outcome="rejected",
                        detail=(
                            f"downloaded file size {actual_size} exceeds "
                            f"{self.config.max_file_bytes}"
                        ),
                        public_message="The voice file is too large. Nothing was sent.",
                    )
                return await self.transcriber.transcribe(audio_path)
        except VoiceInputProblem:
            raise
        except Exception as exc:
            raise VoiceInputProblem(
                outcome="failed",
                detail=str(exc),
                public_message=(
                    "The voice message could not be transcribed. Nothing was sent."
                ),
            ) from exc

    def _validate_metadata(self, media: VoiceMedia) -> None:
        if (
            media.duration_seconds is not None
            and media.duration_seconds > self.config.max_duration_seconds
        ):
            raise VoiceInputProblem(
                outcome="rejected",
                detail=(
                    f"duration {media.duration_seconds}s exceeds "
                    f"{self.config.max_duration_seconds}s"
                ),
                public_message="The voice message is too long. Nothing was sent.",
            )
        if (
            media.advertised_size is not None
            and media.advertised_size > self.config.max_file_bytes
        ):
            raise VoiceInputProblem(
                outcome="rejected",
                detail=(
                    f"file size {media.advertised_size} exceeds "
                    f"{self.config.max_file_bytes}"
                ),
                public_message="The voice file is too large. Nothing was sent.",
            )


def transcription_detail(
    transcriber: VoiceTranscriber,
    transcript: Transcript,
) -> str:
    return (
        f"provider={transcriber.provider_name} "
        f"model={transcriber.model_name or '-'} "
        f"language={transcript.language or '-'}"
    )


def audio_suffix(mime_type: str | None, filename: str | None = None) -> str:
    by_type = {
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get((mime_type or "").split(";", 1)[0].casefold())
    if by_type is not None:
        return by_type
    suffix = Path(filename or "").suffix.casefold()
    if suffix in {".ogg", ".opus", ".mp3", ".m4a", ".webm", ".wav"}:
        return suffix
    return ".ogg"
