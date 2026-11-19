from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from herdrelay import _mlx_whisper_worker

from herdrelay.config import VoiceTranscriptionConfig
from herdrelay.voice_transcription import (
    CommandVoiceTranscriber,
    MlxWhisperTranscriber,
    TranscriptionError,
)


def test_mlx_worker_redirects_library_status_output_to_stderr(
    tmp_path, monkeypatch, capsys
):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    def fake_transcribe(*_args, **_kwargs):
        print("Detected language: Japanese")
        return {"text": "文字起こし", "language": "ja", "segments": []}

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=fake_transcribe),
    )

    result = _mlx_whisper_worker.transcribe(audio, "model", "smart")
    captured = capsys.readouterr()

    assert result["text"] == "文字起こし"
    assert captured.out == ""
    assert "Detected language" in captured.err


@pytest.mark.asyncio
async def test_command_transcriber_replaces_input_and_parses_json(tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    script = (
        "import json,sys; "
        "assert sys.argv[1].endswith('voice.ogg'); "
        "print(json.dumps({'text':'HJB FDMのパイに続けて',"
        "'language':'ja','duration_seconds':2.5}, ensure_ascii=False))"
    )
    transcriber = CommandVoiceTranscriber(
        VoiceTranscriptionConfig(
            provider="command",
            argv=(sys.executable, "-c", script, "{input}"),
            timeout_seconds=2,
        )
    )

    transcript = await transcriber.transcribe(audio)

    assert transcript.text == "HJB FDMのパイに続けて"
    assert transcript.language == "ja"
    assert transcript.duration_seconds == 2.5


@pytest.mark.asyncio
async def test_mlx_transcriber_uses_configured_python_and_sanitized_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    executable = tmp_path / "fake-python"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
assert sys.argv[2].endswith('voice.ogg')
assert sys.argv[3] == 'mlx-community/whisper-large-v3-turbo'
assert sys.argv[4] == 'smart'
assert 'TELEGRAM_BOT_TOKEN' not in os.environ
print(json.dumps({'text': '文字起こし', 'language': 'ja', 'duration_seconds': 1.0}, ensure_ascii=False))
"""
    )
    executable.chmod(0o700)
    transcriber = MlxWhisperTranscriber(
        VoiceTranscriptionConfig(
            provider="mlx-whisper",
            model="mlx-community/whisper-large-v3-turbo",
            python_path=str(executable),
            timeout_seconds=2,
        )
    )

    transcript = await transcriber.transcribe(audio)

    assert transcript.text == "文字起こし"


@pytest.mark.asyncio
async def test_command_transcriber_rejects_non_json_output(tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    transcriber = CommandVoiceTranscriber(
        VoiceTranscriptionConfig(
            provider="command",
            argv=(sys.executable, "-c", "print('not json')"),
            timeout_seconds=2,
        )
    )

    with pytest.raises(TranscriptionError, match="valid JSON"):
        await transcriber.transcribe(audio)
