from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import AppConfig
from .herdr_cli import HerdrCli, HerdrCliError
from .models import HerdrTarget


class TargetResolutionError(ValueError):
    pass


class HerdrClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.cli = HerdrCli(config.herdr)

    def list_targets(self) -> list[HerdrTarget]:
        try:
            return normalize_targets(self.cli.agent_list(), preferred_kind="agent")
        except HerdrCliError:
            return normalize_targets(self.cli.pane_list(), preferred_kind="pane")

    def read(self, target: str, *, lines: int) -> str:
        try:
            return self.cli.agent_read(target, lines=lines)
        except HerdrCliError:
            return self.cli.pane_read(target, lines=lines)

    def send(self, target: str, message: str) -> None:
        try:
            self.cli.agent_send(target, message)
            return
        except HerdrCliError:
            if not self.config.allow_pane_send_fallback:
                raise
        self.cli.pane_run(target, message)

    def send_keys(self, target: str, keys: Iterable[str]) -> None:
        self.cli.pane_send_keys(target, *tuple(keys))

    def send_text_enter(self, target: str, text: str) -> None:
        self.cli.pane_run(target, text)

    def resolve_target(self, query: str) -> HerdrTarget:
        candidates = []
        for target in self.list_targets():
            values = {
                target.target,
                target.label,
                target.agent_name,
                target.workspace_label,
                target.cwd,
            }
            if query in {value for value in values if value}:
                candidates.append(target)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return HerdrTarget(target=query)
        raise TargetResolutionError(f"Target is ambiguous: {query}")


def normalize_targets(payload: Any, *, preferred_kind: str) -> list[HerdrTarget]:
    items = _extract_items(payload, preferred_kind)
    targets = [_target_from_item(item, preferred_kind=preferred_kind) for item in items]
    return [target for target in targets if target.target]


def _extract_items(payload: Any, preferred_kind: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    result = payload.get("result")
    if isinstance(result, dict):
        for key in (f"{preferred_kind}s", "agents", "panes", "items"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for key in ("agent", "pane"):
            value = result.get(key)
            if isinstance(value, dict):
                return [value]
    for key in (f"{preferred_kind}s", "agents", "panes", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def _target_from_item(item: dict[str, Any], *, preferred_kind: str) -> HerdrTarget:
    target = _first_str(
        item,
        "target",
        "agent_id",
        "pane_id",
        "id",
        "terminal_id",
        "label",
    )
    pane = _nested_dict(item, "pane")
    workspace = _nested_dict(item, "workspace")
    tab = _nested_dict(item, "tab")
    agent = _nested_dict(item, "agent")

    return HerdrTarget(
        target=target or "",
        kind=_first_str(item, "kind", "type") or preferred_kind,
        label=_first_str(item, "label", "name")
        or _first_str(pane, "label", "title")
        or _first_str(agent, "label", "name"),
        agent_name=_first_str(item, "agent", "agent_name", "name")
        or _first_str(agent, "agent", "agent_name", "name"),
        status=_first_str(item, "agent_status", "status")
        or _first_str(agent, "agent_status", "status"),
        workspace_label=_first_str(item, "workspace_label")
        or _first_str(workspace, "label", "name"),
        tab_label=_first_str(item, "tab_label") or _first_str(tab, "label", "name"),
        cwd=_first_str(item, "cwd", "foreground_cwd", "working_directory")
        or _first_str(pane, "cwd", "foreground_cwd", "working_directory"),
        raw=item,
    )


def _nested_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _first_str(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None

