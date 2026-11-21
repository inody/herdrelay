from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from .config import AppConfig
from .domain import (
    BackendUnavailableError,
    MutationFailedError,
    MutationOutcomeUnknownError,
    MutationReceipt,
    MutationRejectedError,
    RuntimeKind,
    TargetKey,
)
from .herdr_cli import HerdrCli, HerdrCliError
from .models import HerdrTarget, PendingQuestion
from .questions import QUESTION_KEY_INTERVAL_SECONDS, selection_keys
from .runtime import RuntimeCapabilities


class TargetResolutionError(ValueError):
    pass


class HerdrMutationRejected(MutationRejectedError):
    """Legacy Herdr definitively rejected a prompt before applying it."""


class HerdrMutationFailed(MutationFailedError):
    """Legacy Herdr definitively failed to apply a prompt."""


class HerdrBackendUnavailable(BackendUnavailableError):
    """Legacy Herdr or its CLI was unavailable before prompt submission."""


class HerdrClient:
    capabilities = RuntimeCapabilities()

    def __init__(self, config: AppConfig):
        self.config = config
        self.cli = HerdrCli(config.herdr)

    def list_targets(self) -> list[HerdrTarget]:
        workspace_labels = self.list_workspaces()
        try:
            return self._filter_exposed(normalize_targets(
                self.cli.agent_list(),
                preferred_kind="agent",
                workspace_labels=workspace_labels,
            ))
        except HerdrCliError:
            return self._filter_exposed(normalize_targets(
                self.cli.pane_list(),
                preferred_kind="pane",
                workspace_labels=workspace_labels,
            ))

    def list_panes(self) -> list[HerdrTarget]:
        """List all live panes, including shells without a detected agent.

        Reject malformed snapshots: callers use absence here to archive threads.
        """
        payload = self.cli.pane_list()
        result = payload.get("result", payload) if isinstance(payload, dict) else payload
        items = result.get("panes") if isinstance(result, dict) else result
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("pane_id"), str)
            or not item["pane_id"].strip()
            for item in items
        ):
            raise HerdrCliError("Herdr did not return a valid pane list")
        return self._filter_exposed(normalize_targets(
            items,
            preferred_kind="pane",
            workspace_labels=self.list_workspaces(),
        ))

    def _filter_exposed(self, targets: list[HerdrTarget]) -> list[HerdrTarget]:
        exposure = self.config.runtime.tui.target_exposure
        if exposure.mode == "none":
            return []
        if exposure.mode == "selected":
            return [target for target in targets if target.target in exposure.addresses]
        return targets

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

    def target_key(self, target: str) -> TargetKey:
        return TargetKey.legacy(target)

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget:
        if key.runtime is not RuntimeKind.HERDR_TUI:
            raise TargetResolutionError(
                "Core target identity cannot be used with the legacy Herdr TUI"
            )
        return self.resolve_target(key.address)

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

    def send(self, target: str, message: str) -> MutationReceipt:
        if not message:
            raise HerdrMutationRejected("Herdr prompt must not be empty")
        try:
            self.cli.agent_prompt(target, message)
        except HerdrCliError as exc:
            if "timed out" in str(exc).casefold():
                raise MutationOutcomeUnknownError(str(exc)) from exc
            if not self.config.allow_pane_send_fallback:
                raise _normalize_herdr_send_error(exc) from exc
        else:
            return MutationReceipt(observed_turn=None, result={})
        try:
            self.cli.pane_run(target, message)
        except HerdrCliError as exc:
            if "timed out" in str(exc).casefold():
                raise MutationOutcomeUnknownError(str(exc)) from exc
            raise _normalize_herdr_send_error(exc) from exc
        return MutationReceipt(observed_turn=None, result={"fallback": "pane_run"})

    def _send_question_keys(
        self, target: str, keys: Iterable[str], *, interval_seconds: float = 0
    ) -> None:
        if interval_seconds <= 0:
            self.cli.pane_send_keys(target, *tuple(keys))
            return
        for index, key in enumerate(keys):
            if index:
                time.sleep(interval_seconds)
            self.cli.pane_send_keys(target, key)

    def answer_question(
        self, question: PendingQuestion, selected: tuple[int, ...]
    ) -> None:
        self._send_question_keys(
            question.pane_id,
            selection_keys(question, selected),
            interval_seconds=QUESTION_KEY_INTERVAL_SECONDS,
        )

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


def _normalize_herdr_send_error(exc: HerdrCliError) -> MutationFailedError:
    error_type = (
        HerdrBackendUnavailable
        if "cli not found" in str(exc).casefold()
        else HerdrMutationFailed
    )
    return error_type(str(exc))


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


def _nested_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _first_str(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None
