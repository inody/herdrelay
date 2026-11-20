from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .domain import BackendKind, TargetKey
from .herdr_client import TargetResolutionError
from .models import HerdrTarget
from .orca_cli import OrcaAgent, OrcaCli, OrcaCliError, OrcaTerminal
from .runtime import RuntimeCapabilities


_ACTIVE_TERMINAL_AGENT_STATES = frozenset({"working", "blocked", "waiting"})


@dataclass(frozen=True, slots=True)
class OrcaMutationReceipt:
    observed_turn: int | None
    result: dict[str, Any]


class OrcaClient:
    """RuntimeClient implementation over Orca's public CLI."""

    capabilities = RuntimeCapabilities(normal_send=True, native_interrupt=False)

    def __init__(self, config: AppConfig):
        self.config = config
        self.orca = config.runtime.orca
        self.cli = OrcaCli(self.orca)
        self._runtime_id: str | None = None
        self._targets: dict[str, HerdrTarget] = {}

    def list_targets(self) -> list[HerdrTarget]:
        agent_runtime_id, agents = self.cli.agent_list()
        terminal_runtime_id, terminals = self.cli.terminal_list()
        if terminal_runtime_id != agent_runtime_id:
            raise OrcaCliError(
                "Orca agent and terminal snapshots came from different runtimes"
            )
        self._runtime_id = agent_runtime_id
        targets = {
            target.target: target
            for terminal in terminals
            if (target := self._target_from_terminal(terminal)) is not None
        }
        for agent in agents:
            target = self._target_from_agent(agent)
            if target is None:
                continue
            matching_terminal = _matching_terminal(agent, terminals)
            if agent.kind == "terminal":
                if matching_terminal is not None:
                    targets.pop(
                        f"{matching_terminal.host_id}/{matching_terminal.pane_key}",
                        None,
                    )
                elif (
                    target.target not in targets
                    and agent.state not in _ACTIVE_TERMINAL_AGENT_STATES
                ):
                    continue
            targets[target.target] = target
        self._targets = targets
        return list(targets.values())

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
        for _attempt in range(2):
            targets = self.list_targets()
            if self._runtime_id != key.instance_id:
                raise OrcaCliError(
                    "bound target belongs to a different Orca runtime; "
                    "no action was taken"
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
        resolution_error: OrcaCliError | None = None
        for _attempt in range(2):
            try:
                current = self._resolve_live_target(target)
                if current.status and current.status.casefold() == "blocked":
                    raise OrcaCliError(
                        f"Orca agent is blocked: {target}", kind="blocked"
                    )
                _, terminal = self._resolve_mutation_terminal(
                    target, current=current
                )
                break
            except OrcaCliError as exc:
                if exc.kind:
                    raise
                resolution_error = exc
        else:
            assert resolution_error is not None
            raise resolution_error
        result = self.cli.terminal_send(
            handle=terminal.handle,
            text=message,
            wait_submit_seconds=self.orca.send_wait_seconds,
        )
        return OrcaMutationReceipt(observed_turn=None, result=result)

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
        exact = [
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
        if len(exact) == 1:
            return exact[0]
        if (
            exact
            or not isinstance(worktree_id, str)
            or not isinstance(agent_name, str)
        ):
            raise OrcaCliError(
                "Orca agent did not resolve to exactly one writable terminal"
            )
        matches = [
            terminal
            for terminal in terminals
            if terminal.host_id == host_id
            and terminal.connected
            and terminal.writable
            and terminal.worktree_id == worktree_id
            and terminal.agent_identity is not None
            and terminal.agent_identity.casefold() == agent_name.casefold()
        ]
        if len(matches) != 1:
            raise OrcaCliError(
                "Orca agent did not resolve to exactly one writable terminal"
            )
        return matches[0]

    def _target_from_terminal(
        self, terminal: OrcaTerminal
    ) -> HerdrTarget | None:
        if (
            terminal.host_id not in self.orca.host_ids
            or not self.orca.include_terminal_agents
            or not terminal.agent_identity
            or not self._target_exposed(
                terminal.pane_key, terminal.worktree_id
            )
        ):
            return None
        worktree_path = terminal.raw.get("worktreePath")
        if not isinstance(worktree_path, str) or not worktree_path:
            worktree_path = _worktree_path(terminal.worktree_id)
        title = terminal.raw.get("title")
        title = title if isinstance(title, str) and title else None
        project_label = _path_label(worktree_path)
        address = f"{terminal.host_id}/{terminal.pane_key}"
        raw_agent = {
            "kind": "terminal",
            "agent": terminal.agent_identity,
            "state": "starting",
            "hostId": terminal.host_id,
            "worktreeId": terminal.worktree_id,
            "paneKey": terminal.pane_key,
            "title": title,
            "lastAssistantMessage": None,
            "updatedAt": None,
        }
        return HerdrTarget(
            target=address,
            kind="orca-terminal",
            label=project_label or title or address,
            agent_name=terminal.agent_identity,
            status="starting",
            workspace_label=project_label,
            tab_label=title,
            cwd=worktree_path,
            raw={
                "orca_runtime_id": self._runtime_id,
                "orca_agent": raw_agent,
                "orca_terminal": terminal.raw,
            },
        )

    def _target_exposed(
        self, endpoint: str, worktree_id: str | None
    ) -> bool:
        exposure = self.orca.target_exposure
        if exposure.mode == "all-eligible":
            return True
        if exposure.mode == "none":
            return False
        return endpoint in exposure.pane_keys or bool(
            worktree_id and worktree_id in exposure.worktree_ids
        )

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
        if not self._target_exposed(endpoint, agent.worktree_id):
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


def _matching_terminal(
    agent: OrcaAgent, terminals: list[OrcaTerminal]
) -> OrcaTerminal | None:
    if not agent.pane_key:
        return None
    exact = [
        terminal
        for terminal in terminals
        if terminal.host_id == agent.host_id
        and terminal.pane_key == agent.pane_key
    ]
    if len(exact) == 1:
        return exact[0]
    if exact or not agent.worktree_id or not agent.agent:
        return None
    matches = [
        terminal
        for terminal in terminals
        if terminal.host_id == agent.host_id
        and terminal.worktree_id == agent.worktree_id
        and terminal.agent_identity is not None
        and terminal.agent_identity.casefold() == agent.agent.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _worktree_path(worktree_id: str | None) -> str | None:
    if not worktree_id or "::" not in worktree_id:
        return None
    path = worktree_id.split("::", 1)[1].strip()
    return path or None


def _path_label(path: str | None) -> str | None:
    if not path:
        return None
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name or None


def _project_label(agent: OrcaAgent, worktree_path: str | None) -> str | None:
    for value in (agent.repo, agent.workspace):
        if value and value not in {"~", "-", "main"}:
            return value
    return _path_label(worktree_path) or agent.repo or agent.workspace
