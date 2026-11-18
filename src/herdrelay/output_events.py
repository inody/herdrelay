from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .domain import OutputEventKind, RuntimeKind, TargetKey
from .models import PendingQuestion, QuestionOption

SUPPORTED_OUTPUT_EVENT_VERSIONS = {1, 2}


@dataclass(frozen=True)
class HookOutputEvent:
    version: int
    event_id: str
    agent: str
    target: TargetKey
    text: str
    kind: str = OutputEventKind.RESPONSE.value
    question: PendingQuestion | None = None

    @property
    def pane_id(self) -> str:
        """Legacy compatibility for Discord code that still binds pane IDs."""
        return self.target.address


def load_hook_output_event(path: Path, *, max_bytes: int) -> HookOutputEvent:
    """Load one versioned local output event without guessing target identity."""
    if path.stat().st_size > max_bytes:
        raise ValueError(f"event exceeds {max_bytes} bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("unsupported event format")
    version = payload.get("version")
    if version not in SUPPORTED_OUTPUT_EVENT_VERSIONS:
        raise ValueError("unsupported event format")

    kind = payload.get("kind", OutputEventKind.RESPONSE.value)
    supported_kinds = {item.value for item in OutputEventKind}
    if kind not in supported_kinds:
        raise ValueError("unsupported event kind")

    required = ("event_id", "agent")
    if kind not in {
        OutputEventKind.QUESTION_RESOLVED.value,
        OutputEventKind.HUMAN_INPUT.value,
    }:
        required += ("text",)
    values = {key: payload.get(key) for key in required}
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ValueError(f"{', '.join(required)} must be non-empty strings")
    event_id = values["event_id"]
    agent = values["agent"]
    text = values.get("text", "")
    assert isinstance(event_id, str)
    assert isinstance(agent, str)
    assert isinstance(text, str)
    if len(event_id) > 200 or len(agent) > 50:
        raise ValueError("event identifier or agent name is too long")

    target = _load_target(payload, version=version)
    question = _load_question(
        payload.get("question"),
        pane_id=target.address,
        event_id=event_id,
    )
    if kind == OutputEventKind.QUESTION.value and question is None:
        raise ValueError("question event is missing a valid question")
    return HookOutputEvent(
        version=version,
        event_id=event_id,
        agent=agent,
        target=target,
        text=text,
        kind=kind,
        question=question,
    )


def _load_target(payload: dict, *, version: int) -> TargetKey:
    if version == 1:
        if any(key in payload for key in ("runtime", "core_id", "target")):
            raise ValueError("version 1 event has conflicting Core identity")
        pane_id = payload.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            raise ValueError("pane_id must be a non-empty string")
        if len(pane_id) > 200:
            raise ValueError("target identity is too long")
        return TargetKey.legacy(pane_id)

    if payload.get("runtime") != RuntimeKind.HERDR_CORE.value:
        raise ValueError("version 2 event runtime must be herdr-core")
    if "pane_id" in payload:
        raise ValueError("version 2 event has conflicting legacy identity")
    core_id = payload.get("core_id")
    address = payload.get("target")
    if not isinstance(core_id, str) or not core_id:
        raise ValueError("core_id must be a non-empty string")
    if not isinstance(address, str) or not address:
        raise ValueError("target must be a non-empty string")
    if len(core_id) > 200 or len(address) > 200:
        raise ValueError("target identity is too long")
    return TargetKey.core(controller_core_id=core_id, address=address)


def _load_question(
    value: object, *, pane_id: str, event_id: str
) -> PendingQuestion | None:
    if not isinstance(value, dict):
        return None
    prompt = value.get("prompt")
    options_data = value.get("options")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or not isinstance(options_data, list)
    ):
        return None
    options = tuple(
        QuestionOption(
            label=option["label"].strip(),
            description=(
                option["description"].strip()
                if isinstance(option.get("description"), str)
                and option["description"].strip()
                else None
            ),
        )
        for option in options_data
        if isinstance(option, dict)
        and isinstance(option.get("label"), str)
        and option["label"].strip()
    )
    if not options:
        return None
    return PendingQuestion(
        pane_id=pane_id,
        event_id=event_id,
        prompt=prompt.strip(),
        options=options,
        multi_select=value.get("multi_select") is True,
    )


def inbox_events_in_arrival_order(inbox: Path) -> list[Path]:
    """List complete event files oldest first."""
    stamped: list[tuple[int, str, Path]] = []
    for path in inbox.glob("*.json"):
        try:
            stamped.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:
            continue
    return [path for _mtime, _name, path in sorted(stamped)]
