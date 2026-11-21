from __future__ import annotations

from pathlib import Path

import pytest

from herdrelay.config import VoiceInputConfig
from herdrelay.voice_input import (
    VoiceInputProblem,
    VoiceInputProcessor,
    VoiceMedia,
    audio_suffix,
)
from herdrelay.voice_transcription import Transcript


class FakeTranscriber:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self):
        self.paths: list[Path] = []

    async def transcribe(self, path: Path) -> Transcript:
        assert path.read_bytes() == b"voice"
        self.paths.append(path)
        return Transcript(text="hello", language="en")


@pytest.mark.asyncio
async def test_voice_processor_owns_bounded_temporary_lifecycle():
    transcriber = FakeTranscriber()
    processor = VoiceInputProcessor(
        config=VoiceInputConfig(enabled=True),
        transcriber=transcriber,
    )

    async def download(path: Path, max_bytes: int) -> int:
        assert max_bytes == 10_000_000
        path.write_bytes(b"voice")
        return 5

    transcript = await processor.process(
        VoiceMedia(
            download=download,
            duration_seconds=2,
            advertised_size=5,
            mime_type="audio/ogg",
        ),
        temporary_prefix="test-",
    )

    assert transcript.text == "hello"
    assert len(transcriber.paths) == 1
    assert not transcriber.paths[0].exists()


@pytest.mark.asyncio
async def test_voice_processor_rejects_metadata_before_download():
    called = False

    async def download(path: Path, max_bytes: int) -> int:
        nonlocal called
        called = True
        return 0

    processor = VoiceInputProcessor(
        config=VoiceInputConfig(enabled=True, max_duration_seconds=10),
        transcriber=FakeTranscriber(),
    )

    with pytest.raises(VoiceInputProblem) as captured:
        await processor.process(
            VoiceMedia(download=download, duration_seconds=11),
            temporary_prefix="test-",
        )

    assert captured.value.outcome == "rejected"
    assert called is False


@pytest.mark.asyncio
async def test_voice_processor_rechecks_downloaded_size():
    async def download(path: Path, max_bytes: int) -> int:
        path.write_bytes(b"too large")
        return len(b"too large")

    processor = VoiceInputProcessor(
        config=VoiceInputConfig(enabled=True, max_file_bytes=5),
        transcriber=FakeTranscriber(),
    )

    with pytest.raises(VoiceInputProblem) as captured:
        await processor.process(
            VoiceMedia(download=download),
            temporary_prefix="test-",
        )

    assert captured.value.outcome == "rejected"
    assert "too large" in captured.value.public_message.lower()


def test_audio_suffix_is_shared_across_transports():
    assert audio_suffix("audio/mp4") == ".m4a"
    assert audio_suffix("audio/webm; codecs=opus") == ".webm"
    assert audio_suffix(None, "recording.wav") == ".wav"
    assert audio_suffix("application/octet-stream", "unknown.bin") == ".ogg"
