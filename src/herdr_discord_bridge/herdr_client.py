from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .herdr_cli import HerdrCli, HerdrCliError
from .models import HerdrTarget


class TargetResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class StartedAgent:
    pane_id: str
    result: Any


class HerdrClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.cli = HerdrCli(config.herdr)

    def list_targets(self) -> list[HerdrTarget]:
        workspace_labels = self.list_workspaces()
        try:
            return normalize_targets(
                self.cli.agent_list(),
                preferred_kind="agent",
                workspace_labels=workspace_labels,
            )
        except HerdrCliError:
            return normalize_targets(
                self.cli.pane_list(),
                preferred_kind="pane",
                workspace_labels=workspace_labels,
            )

    def list_workspaces(self) -> dict[str, str]:
        """Return a mapping of workspace_id -> human-readable label."""
        try:
            items = _extract_items(self.cli.workspace_list(), preferred_kind="workspace")
        except HerdrCliError:
            return {}
        mapping: dict[str, str] = {}
        for item in items:
            workspace_id = _first_str(item, "workspace_id", "id")
            label = _first_str(item, "label", "name")
            if workspace_id and label:
                mapping[workspace_id] = label
        return mapping

    def read(
        self,
        target: str,
        *,
        lines: int,
        fmt: str | None = None,
        source: str | None = None,
    ) -> str:
        # All bridge targets are pane IDs. Reading the pane directly remains
        # valid while an agent is being released or re-detected, whereas
        # agent.read can fail during that transition and needlessly churn the
        # server and logs.
        return self.cli.pane_read(target, lines=lines, fmt=fmt, source=source)

    def send(self, target: str, message: str) -> None:
        try:
            self.cli.agent_prompt(target, message)
            return
        except HerdrCliError:
            if not self.config.allow_pane_send_fallback:
                raise
        self.cli.pane_run(target, message)

    def send_keys(self, target: str, keys: Iterable[str]) -> None:
        self.cli.pane_send_keys(target, *tuple(keys))

    def send_text_enter(self, target: str, text: str) -> None:
        self.cli.pane_run(target, text)

    def agent_start(
        self,
        name: str,
        *,
        cwd: str | None = None,
        argv: list[str] | None = None,
        workspace: str | None = None,
        tab: str | None = None,
        split: str | None = None,
        source_pane: str | None = None,
    ) -> StartedAgent:
        """Create a shell pane, then start a Herdr-supported agent in it."""
        direction = split or "right"
        if direction not in {"right", "down"}:
            raise ValueError(f"Unsupported split direction: {direction}")

        if source_pane:
            created = self.cli.pane_split(source_pane, direction=direction, cwd=cwd)
        elif tab:
            panes = _extract_items(self.cli.pane_list(), preferred_kind="pane")
            tab_panes = [pane for pane in panes if _first_str(pane, "tab_id") == tab]
            if not tab_panes:
                raise TargetResolutionError(f"No pane found in tab: {tab}")
            parent = next((pane for pane in tab_panes if pane.get("focused")), tab_panes[0])
            parent_id = _first_str(parent, "pane_id", "id")
            if not parent_id:
                raise TargetResolutionError(f"No usable pane found in tab: {tab}")
            created = self.cli.pane_split(parent_id, direction=direction, cwd=cwd)
        elif workspace:
            created = self.cli.tab_create(workspace=workspace, cwd=cwd)
        else:
            panes = _extract_items(self.cli.pane_list(), preferred_kind="pane")
            focused = next((pane for pane in panes if pane.get("focused")), None)
            focused_id = _first_str(focused or {}, "pane_id", "id")
            if focused_id:
                created = self.cli.pane_split(focused_id, direction=direction, cwd=cwd)
            else:
                created = self.cli.workspace_create(cwd=cwd)

        pane_id = _created_pane_id(created)
        if not pane_id:
            raise HerdrCliError("Herdr did not return a pane ID after creating a pane")

        # Herdr 0.8 starts the canonical executable selected by --kind.  Keep
        # accepting the old `-- <executable> <args...>` form by dropping a
        # redundant executable name from argv.
        agent_args = list(argv or ())
        if agent_args and agent_args[0].casefold() == name.casefold():
            agent_args.pop(0)
        result = self.cli.agent_start(
            name,
            kind=name.casefold(),
            pane_id=pane_id,
            argv=agent_args,
        )
        return StartedAgent(pane_id=pane_id, result=result)

    def pane_close(self, pane_id: str) -> None:
        self.cli.pane_close(pane_id)

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


def normalize_targets(
    payload: Any,
    *,
    preferred_kind: str,
    workspace_labels: dict[str, str] | None = None,
) -> list[HerdrTarget]:
    workspace_labels = workspace_labels or {}
    items = _extract_items(payload, preferred_kind)
    targets = [
        _target_from_item(item, preferred_kind=preferred_kind, workspace_labels=workspace_labels)
        for item in items
    ]
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


def _target_from_item(
    item: dict[str, Any],
    *,
    preferred_kind: str,
    workspace_labels: dict[str, str] | None = None,
) -> HerdrTarget:
    workspace_labels = workspace_labels or {}
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
    workspace_id = (
        _first_str(item, "workspace_id", "workspace")
        or _first_str(workspace, "id", "workspace_id")
    )

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
        workspace_label=(
            _first_str(item, "workspace_label")
            or (workspace_labels.get(workspace_id) if workspace_id else None)
            or _first_str(workspace, "label", "name")
        ),
        tab_label=_first_str(item, "tab_label") or _first_str(tab, "label", "name"),
        cwd=_first_str(item, "cwd", "foreground_cwd", "working_directory")
        or _first_str(pane, "cwd", "foreground_cwd", "working_directory"),
        raw=item,
    )


def _created_pane_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    containers = [result, payload] if isinstance(result, dict) else [payload]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("pane", "root_pane"):
            pane = container.get(key)
            if isinstance(pane, dict):
                pane_id = _first_str(pane, "pane_id", "id")
                if pane_id:
                    return pane_id
        pane_id = _first_str(container, "pane_id")
        if pane_id:
            return pane_id
    return None


def _nested_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _first_str(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None
