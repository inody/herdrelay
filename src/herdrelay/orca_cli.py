from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import OrcaRuntimeConfig


class OrcaCliError(RuntimeError):
    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class OrcaStatus:
    runtime_id: str
    reachable: bool
    state: str
    app_version: str | None = None


@dataclass(frozen=True, slots=True)
class OrcaAgent:
    session_id: str
    kind: str
    agent: str | None
    state: str
    host_id: str
    worktree_id: str | None
    pane_key: str | None
    workspace: str | None
    repo: str | None
    title: str | None
    last_assistant_message: str | None
    updated_at: int | None
    raw: dict[str, Any]


class OrcaCli:
    def __init__(self, config: OrcaRuntimeConfig):
        self.config = config

    def status(self) -> OrcaStatus:
        envelope = self._json(["status"])
        result = _object(envelope.get("result"), "Orca status result")
        runtime = _object(result.get("runtime"), "Orca runtime status")
        runtime_id = _string(runtime, "runtimeId", "Orca runtime ID")
        reachable = runtime.get("reachable")
        if not isinstance(reachable, bool):
            raise OrcaCliError("Orca runtime reachable must be a boolean")
        return OrcaStatus(
            runtime_id=runtime_id,
            reachable=reachable,
            state=_string(runtime, "state", "Orca runtime state"),
            app_version=_optional_string(runtime, "appVersion"),
        )

    def agent_list(self) -> tuple[str, list[OrcaAgent]]:
        envelope = self._json(["agent", "list"])
        runtime_id = _runtime_id(envelope)
        result = _object(envelope.get("result"), "Orca agent list result")
        items = result.get("agents")
        if not isinstance(items, list):
            raise OrcaCliError("Orca agent list must contain an agents array")
        agents = [self._parse_agent(item) for item in items]
        return runtime_id, agents

    def _parse_agent(self, value: Any) -> OrcaAgent:
        item = _object(value, "Orca agent")
        updated_at = item.get("updatedAt")
        if updated_at is not None and (
            not isinstance(updated_at, int) or isinstance(updated_at, bool)
        ):
            raise OrcaCliError("Orca agent updatedAt must be an integer or null")
        return OrcaAgent(
            session_id=_string(item, "orcaSessionId", "Orca session ID"),
            kind=_string(item, "kind", "Orca agent kind").casefold(),
            agent=_optional_string(item, "agent"),
            state=_string(item, "state", "Orca agent state").casefold(),
            host_id=_string(item, "hostId", "Orca host ID"),
            worktree_id=_optional_string(item, "worktreeId"),
            pane_key=_optional_string(item, "paneKey"),
            workspace=_optional_string(item, "workspace"),
            repo=_optional_string(item, "repo"),
            title=_optional_string(item, "title"),
            last_assistant_message=_optional_string(item, "lastAssistantMessage"),
            updated_at=updated_at,
            raw=item,
        )

    def _json(self, args: list[str]) -> dict[str, Any]:
        command = [
            self.config.cli_path,
            *args,
            "--environment",
            self.config.environment,
            "--json",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise OrcaCliError(f"Orca CLI not found: {self.config.cli_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OrcaCliError(f"Orca command timed out: {' '.join(command)}") from exc

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise OrcaCliError(
                f"Orca did not return JSON for {' '.join(command)}: {detail}"
            ) from exc
        if not isinstance(payload, dict):
            raise OrcaCliError("Orca response must be an object")
        if completed.returncode != 0 or payload.get("ok") is not True:
            error = payload.get("error")
            kind = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            detail = message or completed.stderr.strip() or "Orca command failed"
            raise OrcaCliError(str(detail), kind=str(kind) if kind else None)
        return payload


def _runtime_id(envelope: dict[str, Any]) -> str:
    meta = _object(envelope.get("_meta"), "Orca response metadata")
    return _string(meta, "runtimeId", "Orca runtime ID")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrcaCliError(f"{label} must be an object")
    return value


def _string(item: dict[str, Any], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OrcaCliError(f"{label} must be a non-empty string")
    return value


def _optional_string(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OrcaCliError(f"Orca {key} must be a string or null")
    return value
