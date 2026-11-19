from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Protocol

from .config import VoiceTranscriptionConfig
from .formatter import truncate


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


class VoiceTranscriber(Protocol):
    provider_name: str
    model_name: str | None

    async def transcribe(self, path: Path) -> Transcript: ...


class MlxWhisperTranscriber:
    provider_name = "mlx-whisper"

    def __init__(self, config: VoiceTranscriptionConfig):
        if not config.model or not config.python_path:
            raise ValueError("mlx-whisper requires model and python_path")
        self.config = config
        self.model_name = config.model

    async def transcribe(self, path: Path) -> Transcript:
        worker = Path(__file__).with_name("_mlx_whisper_worker.py")
        stdout = await _run_transcriber(
            (
                self.config.python_path,
                str(worker),
                str(path),
                self.config.model or "",
                self.config.language,
            ),
            config=self.config,
            label="mlx-whisper",
            env=_transcription_environment(self.config),
        )
        return _parse_transcript(stdout)


class CommandVoiceTranscriber:
    provider_name = "command"

    def __init__(self, config: VoiceTranscriptionConfig):
        if not config.argv:
            raise ValueError("Command transcription requires argv")
        self.config = config
        self.model_name = config.model

    async def transcribe(self, path: Path) -> Transcript:
        replaced = False
        argv: list[str] = []
        for item in self.config.argv:
            if "{input}" in item:
                replaced = True
                item = item.replace("{input}", str(path))
            argv.append(item)
        if not replaced:
            argv.append(str(path))
        stdout = await _run_transcriber(
            tuple(argv),
            config=self.config,
            label="Transcription command",
        )
        return _parse_transcript(stdout)


def build_voice_transcriber(config: VoiceTranscriptionConfig) -> VoiceTranscriber:
    if config.provider == "mlx-whisper":
        return MlxWhisperTranscriber(config)
    if config.provider == "command":
        return CommandVoiceTranscriber(config)
    raise ValueError(f"Unsupported voice transcription provider: {config.provider}")


async def _run_transcriber(
    argv: tuple[str, ...],
    *,
    config: VoiceTranscriptionConfig,
    label: str,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise TranscriptionError(f"{label} could not start: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=config.timeout_seconds
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise TranscriptionError(f"{label} timed out") from exc
    if len(stdout) > config.max_output_bytes:
        raise TranscriptionError(f"{label} output exceeded the size limit")
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise TranscriptionError(
            f"{label} failed"
            + (f": {truncate(detail, max_chars=300)}" if detail else "")
        )
    return stdout


def _parse_transcript(value: bytes) -> Transcript:
    try:
        raw = json.loads(value.decode(errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptionError("Transcriber output was not valid JSON") from exc
    if not isinstance(raw, dict):
        raise TranscriptionError("Transcriber output must be an object")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TranscriptionError("Transcriber returned an empty transcript")
    language = raw.get("language")
    if language is not None and not isinstance(language, str):
        raise TranscriptionError("Transcriber language must be a string or null")
    duration = raw.get("duration_seconds")
    if duration is not None and (
        not isinstance(duration, (int, float)) or isinstance(duration, bool)
    ):
        raise TranscriptionError("Transcriber duration must be numeric or null")
    return Transcript(
        text=text.strip(),
        language=language,
        duration_seconds=float(duration) if duration is not None else None,
    )


def _transcription_environment(
    config: VoiceTranscriptionConfig,
) -> dict[str, str]:
    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "XDG_CACHE_HOME",
        "HF_HOME",
        "HUGGINGFACE_HUB_CACHE",
    }
    allowed.update(config.pass_env)
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    resolved_python = shutil.which(config.python_path)
    path_entries = [
        os.path.dirname(resolved_python or config.python_path),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    env["PATH"] = ":".join(dict.fromkeys(entry for entry in path_entries if entry))
    return env
