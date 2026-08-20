from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HerdrTarget:
    target: str
    kind: str = "unknown"
    label: str | None = None
    agent_name: str | None = None
    status: str | None = None
    workspace_label: str | None = None
    tab_label: str | None = None
    cwd: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class Binding:
    guild_id: str
    channel_id: str
    thread_id: str | None
    herdr_target: str
    label: str | None
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuditEntry:
    discord_user_id: str
    action: str
    result: str
    guild_id: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    herdr_target: str | None = None
    payload_preview: str | None = None


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str | None = None


@dataclass(frozen=True)
class PendingQuestion:
    pane_id: str
    event_id: str
    prompt: str
    options: tuple[QuestionOption, ...]
    multi_select: bool = False

