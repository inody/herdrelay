#!/usr/bin/env python3
"""Forward agent responses and Claude questions to HerdRelay.

This hook is intentionally silent and best-effort: relay failures must never
change the agent's decision or write anything into its transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

DEFAULT_INBOX = "~/.cache/herdrelay/agent-output"


def resolve_pane_id(
    environ: dict[str, str], *, allow_herdr_lookup: bool = False, cwd: object = None
) -> str:
    """Find the Herdr pane that owns a hook invocation.

    A standalone agent inherits ``HERDR_PANE_ID``. Codex's shared app-server
    does not, so when its current working directory identifies exactly one
    Codex pane, use Herdr's metadata (never terminal output) as a fallback.
    """
    pane_id = environ.get("HERDR_PANE_ID", "").strip()
    if pane_id or not allow_herdr_lookup or not isinstance(cwd, str) or not cwd:
        return pane_id
    try:
        result = subprocess.run(
            ["herdr", "pane", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
        payload = json.loads(result.stdout)
        panes = payload.get("result", {}).get("panes", [])
        target_cwd = str(Path(cwd).resolve())
        matches = [
            pane.get("pane_id", "")
            for pane in panes
            if isinstance(pane, dict)
            and pane.get("agent") == "codex"
            and isinstance(pane.get("cwd"), str)
            and str(Path(pane["cwd"]).resolve()) == target_cwd
            and isinstance(pane.get("pane_id"), str)
            and pane["pane_id"].strip()
        ]
        return matches[0] if len(matches) == 1 else ""
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return ""


def build_event(
    data: object, environ: dict[str, str], *, agent: str = "claude"
) -> dict[str, object] | None:
    if not isinstance(data, dict) or data.get("hook_event_name") != "Stop":
        return None
    pane_id = resolve_pane_id(
        environ,
        allow_herdr_lookup=agent == "codex",
        cwd=data.get("cwd"),
    )
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


def build_question_event(
    data: object, environ: dict[str, str]
) -> dict[str, object] | None:
    """Build a relay event from Claude's AskUserQuestion PreToolUse payload."""
    if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return None
    if data.get("tool_name") != "AskUserQuestion":
        return None
    pane_id = resolve_pane_id(environ)
    tool_input = data.get("tool_input")
    text = format_ask_user_question(tool_input)
    question = first_ask_user_question(tool_input)
    if not pane_id or not text or question is None:
        return None
    session_id = data.get("session_id")
    tool_use_id = data.get("tool_use_id")
    if not isinstance(session_id, str):
        session_id = ""
    if not isinstance(tool_use_id, str):
        tool_use_id = ""
    identity = "\0".join(("claude-question", session_id, tool_use_id, text))
    return {
        "version": 1,
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "agent": "claude",
        "pane_id": pane_id,
        "session_id": session_id,
        "kind": "question",
        "question": question,
        "text": text,
        "created_at": time.time(),
    }


def first_ask_user_question(tool_input: object) -> dict[str, object] | None:
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        return None
    item = questions[0]
    if not isinstance(item, dict):
        return None
    prompt = item.get("question")
    options = item.get("options")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(options, list):
        return None
    normalized_options = [
        {
            "label": option["label"].strip(),
            "description": (
                option["description"].strip()
                if isinstance(option.get("description"), str)
                and option["description"].strip()
                else None
            ),
        }
        for option in options
        if isinstance(option, dict)
        and isinstance(option.get("label"), str)
        and option["label"].strip()
    ]
    if not normalized_options:
        return None
    return {
        "prompt": prompt.strip(),
        "options": normalized_options,
        "multi_select": item.get("multiSelect") is True,
    }


def format_ask_user_question(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return ""
    rendered: list[str] = []
    for item in questions:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        block = question.strip()
        options = item.get("options")
        if isinstance(options, list):
            choices: list[str] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                label = option.get("label")
                description = option.get("description")
                if not isinstance(label, str) or not label.strip():
                    continue
                choice = f"- {label.strip()}"
                if isinstance(description, str) and description.strip():
                    choice += f": {description.strip()}"
                choices.append(choice)
            if choices:
                block += "\nOptions:\n" + "\n".join(choices)
        if item.get("multiSelect") is True:
            block += "\n(Multiple selections allowed.)"
        rendered.append(block)
    return "\n\n".join(rendered)


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
    parser.add_argument("--event", choices=("stop", "question"), default="stop")
    args, _unknown = parser.parse_known_args()
    try:
        data = json.load(sys.stdin)
        event = (
            build_question_event(data, dict(os.environ))
            if args.event == "question"
            else build_event(data, dict(os.environ), agent=args.agent)
        )
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
