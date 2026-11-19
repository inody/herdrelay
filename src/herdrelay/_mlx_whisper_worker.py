from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from typing import Any


def transcript_duration(result: dict[str, Any]) -> float | None:
    segments = result.get("segments") or []
    if not segments:
        return None
    try:
        return float(segments[-1]["end"])
    except (KeyError, TypeError, ValueError):
        return None


def should_retry_in_japanese(result: dict[str, Any]) -> bool:
    detected = str(result.get("language") or "").casefold()
    duration = transcript_duration(result)
    return detected not in {"ja", "en"} and duration is not None and duration <= 120


def transcribe(path: Path, model: str, language: str) -> dict[str, Any]:
    import mlx_whisper

    requested = None if language in {"", "auto", "none", "smart"} else language
    with redirect_stdout(sys.stderr):
        result = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=model,
            language=requested,
            verbose=False,
        )
    if language == "smart" and should_retry_in_japanese(result):
        with redirect_stdout(sys.stderr):
            result = mlx_whisper.transcribe(
                str(path),
                path_or_hf_repo=model,
                language="ja",
                verbose=False,
            )
    return result


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: worker AUDIO MODEL LANGUAGE", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    result = transcribe(path, sys.argv[2], sys.argv[3].casefold())
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        print("mlx-whisper returned an empty transcript", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "text": text.strip(),
                "language": result.get("language"),
                "duration_seconds": transcript_duration(result),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
