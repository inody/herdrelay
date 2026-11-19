from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import AppConfig
from .domain import BackendKind, TargetKey
from .herdr_client import TargetResolutionError
from .models import HerdrTarget, StartedAgent
from .orca_cli import OrcaAgent, OrcaCli, OrcaCliError
from .runtime import RuntimeCapabilities


class OrcaUnsupportedOperation(OrcaCliError):
    pass


class OrcaClient:
    """Read-only RuntimeClient compatibility layer over Orca's public CLI."""

    capabilities = RuntimeCapabilities(
        normal_send=False,
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
        del target, message
        raise OrcaUnsupportedOperation(
            "sending prompts through the Orca backend is not implemented"
        )

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

    def _target_from_agent(self, agent: OrcaAgent) -> HerdrTarget | None:
        if agent.host_id not in self.orca.allowed_host_ids:
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
        if (
            endpoint not in self.orca.allowed_pane_keys
            and (not agent.worktree_id or agent.worktree_id not in self.orca.allowed_worktree_ids)
        ):
            return None

        address = f"{agent.host_id}/{endpoint}"
        raw: dict[str, Any] = {
            "orca_runtime_id": self._runtime_id,
            "orca_agent": agent.raw,
        }
        return HerdrTarget(
            target=address,
            kind=f"orca-{agent.kind}",
            label=agent.workspace or agent.repo or agent.title or address,
            agent_name=agent.agent,
            status=agent.state,
            workspace_label=agent.workspace or agent.repo,
            tab_label=agent.title,
            raw=raw,
        )
