from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .domain import BackendKind, TargetKey
from .herdr_client import TargetResolutionError
from .models import HerdrTarget, StartedAgent
from .orca_cli import OrcaAgent, OrcaCli, OrcaCliError, OrcaTerminal
from .runtime import RuntimeCapabilities


class OrcaUnsupportedOperation(OrcaCliError):
    pass


@dataclass(frozen=True, slots=True)
class OrcaMutationReceipt:
    observed_turn: int | None
    result: dict[str, Any]


class OrcaClient:
    """Read-only RuntimeClient compatibility layer over Orca's public CLI."""

    capabilities = RuntimeCapabilities(
        normal_send=True,
        bounded_read=True,
        native_events=False,
        native_interrupt=False,
        raw_input=False,
        guarded_approval=False,
        agent_start=False,
        target_close=False,
    )

    def __init__(self, config: AppConfig):
        self.config = config
        self.orca = config.runtime.orca
        self.cli = OrcaCli(self.orca)
        self._runtime_id: str | None = None
        self._targets: dict[str, HerdrTarget] = {}

    def list_targets(self) -> list[HerdrTarget]:
        runtime_id, agents = self.cli.agent_list()
        self._runtime_id = runtime_id
        targets = [
            target
            for agent in agents
            if (target := self._target_from_agent(agent)) is not None
        ]
        self._targets = {target.target: target for target in targets}
        return targets

    def list_panes(self) -> list[HerdrTarget]:
        return self.list_targets()

    def target_key(self, target: str) -> TargetKey:
        if target not in self._targets:
            self.list_targets()
        item = self._targets.get(target)
        if item is None or self._runtime_id is None:
            raise OrcaCliError(f"Orca target is not currently available: {target}")
        return TargetKey.orca(instance_id=self._runtime_id, address=target)

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget:
        if key.backend is not BackendKind.ORCA:
            raise OrcaCliError(
                f"{key.backend.value} target identity cannot be used with Orca"
            )
        targets = self.list_targets()
        if self._runtime_id != key.instance_id:
            raise OrcaCliError(
                "bound target belongs to a different Orca runtime; no action was taken"
            )
        for target in targets:
            if target.target == key.address:
                return target
        raise OrcaCliError(f"Orca target is no longer available: {key.address}")

    def read(
        self,
        target: str,
        *,
        lines: int,
        fmt: str | None = None,
        source: str | None = None,
    ) -> str:
        del fmt, source
        if lines <= 0:
            raise ValueError("Orca read lines must be positive")
        if target not in self._targets:
            self.list_targets()
        item = self._targets.get(target)
        if item is None:
            raise OrcaCliError(f"Orca target is not currently available: {target}")
        raw_agent = item.raw.get("orca_agent") if item.raw else None
        text = (
            raw_agent.get("lastAssistantMessage")
            if isinstance(raw_agent, dict)
            else None
        )
        if not isinstance(text, str):
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def send(self, target: str, message: str) -> None:
        self.send_with_receipt(target, message)

    def send_with_receipt(
        self, target: str, message: str
    ) -> OrcaMutationReceipt:
        if not message:
            raise ValueError("Orca prompt must not be empty")
        current = self._resolve_live_target(target)
        if current.status and current.status.casefold() == "blocked":
            raise OrcaCliError(f"Orca agent is blocked: {target}", kind="blocked")
        _, terminal = self._resolve_mutation_terminal(target, current=current)
        result = self.cli.terminal_send(
            handle=terminal.handle,
            text=message,
            wait_submit_seconds=self.orca.send_wait_seconds,
        )
        return OrcaMutationReceipt(observed_turn=None, result=result)

    def send_keys(
        self, target: str, keys: Iterable[str], *, interval_seconds: float = 0
    ) -> None:
        del target, keys, interval_seconds
        raise OrcaUnsupportedOperation(
            "raw key input is disabled for the Orca backend"
        )

    def send_raw_text(self, target: str, text: str) -> None:
        del target, text
        raise OrcaUnsupportedOperation(
            "raw text input is disabled for the Orca backend"
        )

    def send_text_enter(self, target: str, text: str) -> None:
        del target, text
        raise OrcaUnsupportedOperation(
            "approval text input is disabled for the Orca backend"
        )

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
        del name, cwd, argv, workspace, tab, split, source_pane
        raise OrcaUnsupportedOperation(
            "starting Orca agents from chat is not implemented"
        )

    def pane_close(self, pane_id: str) -> None:
        del pane_id
        raise OrcaUnsupportedOperation(
            "closing Orca terminals from chat is not implemented"
        )

    def resolve_target(self, query: str) -> HerdrTarget:
        candidates = []
        for target in self.list_targets():
            values = {
                target.target,
                target.label,
                target.agent_name,
                target.workspace_label,
                target.tab_label,
            }
            if query in {value for value in values if value}:
                candidates.append(target)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return HerdrTarget(target=query)
        raise TargetResolutionError(f"Target is ambiguous: {query}")

    def _resolve_live_target(self, address: str) -> HerdrTarget:
        self.list_targets()
        target = self._targets.get(address)
        if target is None:
            raise OrcaCliError(f"Orca target is not currently available: {address}")
        return target

    def _resolve_mutation_terminal(
        self, address: str, *, current: HerdrTarget | None = None
    ) -> tuple[HerdrTarget, OrcaTerminal]:
        current = current or self._resolve_live_target(address)
        raw_agent = current.raw.get("orca_agent") if current.raw else None
        if not isinstance(raw_agent, dict):
            raise OrcaCliError(f"Orca target metadata is unavailable: {address}")
        runtime_id, terminals = self.cli.terminal_list()
        if runtime_id != self._runtime_id:
            raise OrcaCliError(
                "Orca runtime identity changed before mutation"
            )
        return current, self._resolve_terminal(raw_agent, terminals)

    def _resolve_terminal(
        self, raw_agent: dict[str, Any], terminals: list[OrcaTerminal]
    ) -> OrcaTerminal:
        pane_key = raw_agent.get("paneKey")
        host_id = raw_agent.get("hostId")
        worktree_id = raw_agent.get("worktreeId")
        agent_name = raw_agent.get("agent")
        if not isinstance(pane_key, str) or not isinstance(host_id, str):
            raise OrcaCliError("Orca agent lacks an exact terminal identity")
        matches = [
            terminal
            for terminal in terminals
            if terminal.host_id == host_id
            and terminal.pane_key == pane_key
            and terminal.connected
            and terminal.writable
            and (
                not isinstance(worktree_id, str)
                or terminal.worktree_id == worktree_id
            )
            and (
                not isinstance(agent_name, str)
                or not terminal.agent_identity
                or terminal.agent_identity.casefold() == agent_name.casefold()
            )
        ]
        if len(matches) != 1:
            raise OrcaCliError(
                "Orca agent did not resolve to exactly one writable terminal"
            )
        return matches[0]

    def _target_from_agent(self, agent: OrcaAgent) -> HerdrTarget | None:
        if agent.host_id not in self.orca.host_ids:
            return None
        if agent.kind == "terminal":
            if not self.orca.include_terminal_agents or not agent.pane_key:
                return None
            endpoint = agent.pane_key
        elif agent.kind == "chat":
            if not self.orca.include_chats:
                return None
            endpoint = f"session:{agent.session_id}"
        else:
            return None
        exposure = self.orca.target_exposure
        if exposure.mode == "none":
            return None
        if exposure.mode == "selected" and (
            endpoint not in exposure.pane_keys
            and (
                not agent.worktree_id
                or agent.worktree_id not in exposure.worktree_ids
            )
        ):
            return None

        address = f"{agent.host_id}/{endpoint}"
        worktree_path = _worktree_path(agent.worktree_id)
        project_label = _project_label(agent, worktree_path)
        raw: dict[str, Any] = {
            "orca_runtime_id": self._runtime_id,
            "orca_agent": agent.raw,
        }
        return HerdrTarget(
            target=address,
            kind=f"orca-{agent.kind}",
            label=project_label or agent.title or address,
            agent_name=agent.agent,
            status=agent.state,
            workspace_label=project_label,
            tab_label=agent.title,
            cwd=worktree_path,
            raw=raw,
        )


def _worktree_path(worktree_id: str | None) -> str | None:
    if not worktree_id or "::" not in worktree_id:
        return None
    path = worktree_id.split("::", 1)[1].strip()
    return path or None


def _project_label(agent: OrcaAgent, worktree_path: str | None) -> str | None:
    for value in (agent.repo, agent.workspace):
        if value and value not in {"~", "-", "main"}:
            return value
    if worktree_path:
        name = worktree_path.rstrip("/").rsplit("/", 1)[-1]
        if name:
            return name
    return agent.repo or agent.workspace
