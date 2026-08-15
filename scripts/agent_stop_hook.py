#!/usr/bin/env python3
"""Forward a completed Claude Code or Codex response to HerdRelay.

This hook is intentionally silent and best-effort: relay failures must never
change the agent's stop decision or write anything into its transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

DEFAULT_INBOX = "~/.cache/herdrelay/agent-output"


def build_event(
    data: object, environ: dict[str, str], *, agent: str = "claude"
) -> dict[str, object] | None:
    if not isinstance(data, dict) or data.get("hook_event_name") != "Stop":
        return None
    pane_id = environ.get("HERDR_PANE_ID", "").strip()
    message = data.get("last_assistant_message")
    session_id = data.get("session_id")
    if not pane_id or not isinstance(message, str) or not message.strip():
        return None
    if not isinstance(session_id, str):
        session_id = ""

    turn_id = data.get("turn_id")
    if not isinstance(turn_id, str):
        turn_id = ""
    transcript_marker = _transcript_marker(data.get("transcript_path"))
    identity = "\0".join((agent, session_id, turn_id, transcript_marker, message))
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "version": 1,
        "event_id": event_id,
        "agent": agent,
        "pane_id": pane_id,
        "session_id": session_id,
        "text": message,
        "created_at": time.time(),
    }


def _transcript_marker(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        stat = Path(value).stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def write_event(event: dict[str, object], inbox: Path) -> Path:
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        inbox.chmod(0o700)
    except OSError:
        pass
    destination = inbox / f"{event['event_id']}.json"
    temporary = inbox / f".{event['event_id']}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(event, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    args, _unknown = parser.parse_known_args()
    try:
        data = json.load(sys.stdin)
        event = build_event(data, dict(os.environ), agent=args.agent)
        if event is not None:
            inbox = Path(
                os.environ.get("HERDRELAY_HOOK_INBOX", DEFAULT_INBOX)
            ).expanduser()
            write_event(event, inbox)
    except BaseException:
        # Never block or add output to the user's agent turn.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
