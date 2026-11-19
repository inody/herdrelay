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


def resolve_event_identity(
    environ: dict[str, str], *, allow_herdr_lookup: bool = False, cwd: object = None
) -> dict[str, object] | None:
    """Resolve one unambiguous legacy, Core, or Orca terminal identity."""
    core_target = environ.get("HC_TERMINAL_ID", "").strip()
    if core_target:
        core_id = environ.get("HERDRELAY_CORE_ID", "").strip()
        if not core_id:
            socket = environ.get("HC_TERMINAL_SOCKET", "").strip()
            if socket:
                try:
                    payload = json.loads(
                        (Path(socket).parent / "core.json").read_text(encoding="utf-8")
                    )
                    value = payload.get("id") if isinstance(payload, dict) else None
                    core_id = value.strip() if isinstance(value, str) else ""
                except (OSError, ValueError, TypeError):
                    core_id = ""
        if not core_id:
            return None
        return {
            "version": 2,
            "runtime": "herdr-core",
            "core_id": core_id,
            "target": core_target,
        }

    orca_pane_key = environ.get("ORCA_PANE_KEY", "").strip()
    if orca_pane_key:
        return resolve_orca_identity(environ, pane_key=orca_pane_key)

    pane_id = resolve_pane_id(
        environ,
        allow_herdr_lookup=allow_herdr_lookup,
        cwd=cwd,
    )
    if not pane_id:
        return None
    return {"version": 1, "pane_id": pane_id}


def resolve_orca_identity(
    environ: dict[str, str], *, pane_key: str
) -> dict[str, object] | None:
    """Resolve an Orca hook pane through the public ``agent list`` CLI."""
    cli_path = environ.get("HERDRELAY_ORCA_CLI", "orca").strip() or "orca"
    environment = (
        environ.get("HERDRELAY_ORCA_ENVIRONMENT", "local").strip() or "local"
    )
    try:
        result = subprocess.run(
            [cli_path, "agent", "list", "--environment", environment, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    metadata = payload.get("_meta")
    runtime_id = metadata.get("runtimeId") if isinstance(metadata, dict) else None
    body = payload.get("result")
    agents = body.get("agents") if isinstance(body, dict) else None
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        return None
    if not isinstance(agents, list):
        return None
    matches = [
        item
        for item in agents
        if isinstance(item, dict)
        and item.get("kind") == "terminal"
        and item.get("paneKey") == pane_key
        and isinstance(item.get("hostId"), str)
        and item["hostId"].strip()
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    host_id = match["hostId"].strip()
    identity: dict[str, object] = {
        "version": 3,
        "backend": "orca",
        "instance_id": runtime_id.strip(),
        "target": f"{host_id}/{pane_key}",
    }
    session_id = match.get("orcaSessionId")
    if isinstance(session_id, str) and session_id.strip():
        identity["generation"] = session_id.strip()
    return identity


def build_event(
    data: object, environ: dict[str, str], *, agent: str = "claude"
) -> dict[str, object] | None:
    if not isinstance(data, dict) or data.get("hook_event_name") != "Stop":
        return None
    target_identity = resolve_event_identity(
        environ,
        allow_herdr_lookup=agent == "codex",
        cwd=data.get("cwd"),
    )
    # Orca's in-process output source is authoritative for completed responses.
    # Keep Claude hooks for question lifecycle only so a Stop event and the
    # public-CLI completion snapshot cannot deliver the same response twice.
    if target_identity is not None and target_identity.get("backend") == "orca":
        return None
    message = data.get("last_assistant_message")
    session_id = data.get("session_id")
    if target_identity is None or not isinstance(message, str) or not message.strip():
        return None
    if not isinstance(session_id, str):
        session_id = ""

    turn_id = data.get("turn_id")
    if not isinstance(turn_id, str):
        turn_id = ""
    transcript_marker = _transcript_marker(data.get("transcript_path"))
    identity = "\0".join(
        (
            agent,
            session_id,
            turn_id,
            transcript_marker,
            json.dumps(target_identity, sort_keys=True),
            message,
        )
    )
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        **target_identity,
        "event_id": event_id,
        "agent": agent,
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
    target_identity = resolve_event_identity(environ)
    tool_input = data.get("tool_input")
    text = format_ask_user_question(tool_input)
    question = first_ask_user_question(tool_input)
    if target_identity is None or not text or question is None:
        return None
    session_id = data.get("session_id")
    tool_use_id = data.get("tool_use_id")
    if not isinstance(session_id, str):
        session_id = ""
    if not isinstance(tool_use_id, str):
        tool_use_id = ""
    identity = "\0".join(
        (
            "claude-question",
            session_id,
            tool_use_id,
            json.dumps(target_identity, sort_keys=True),
            text,
        )
    )
    return {
        **target_identity,
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "agent": "claude",
        "session_id": session_id,
        "kind": "question",
        "question": question,
        "text": text,
        "created_at": time.time(),
    }


def build_question_resolved_event(
    data: object, environ: dict[str, str]
) -> dict[str, object] | None:
    """Build a relay event saying the pane's Claude question is no longer open.

    PostToolUse fires once AskUserQuestion is answered in any way. A rejected
    question never reaches PostToolUse, so UserPromptSubmit also resolves it.
    """
    if not isinstance(data, dict):
        return None
    event_name = data.get("hook_event_name")
    if event_name == "PostToolUse":
        if data.get("tool_name") != "AskUserQuestion":
            return None
    elif event_name != "UserPromptSubmit":
        return None
    target_identity = resolve_event_identity(environ)
    if target_identity is None:
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str):
        session_id = ""
    # Each resolution is a distinct occurrence, even for a repeated prompt.
    identity = "\0".join(
        ("claude-question-resolved", session_id, str(event_name), str(time.time_ns()))
    )
    return {
        **target_identity,
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "agent": "claude",
        "session_id": session_id,
        "kind": "question_resolved",
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
    parser.add_argument(
        "--event", choices=("stop", "question", "question-resolved"), default="stop"
    )
    args, _unknown = parser.parse_known_args()
    try:
        data = json.load(sys.stdin)
        environ = dict(os.environ)
        if args.event == "question":
            event = build_question_event(data, environ)
        elif args.event == "question-resolved":
            event = build_question_resolved_event(data, environ)
        else:
            event = build_event(data, environ, agent=args.agent)
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
