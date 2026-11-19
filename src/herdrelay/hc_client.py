from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .domain import RuntimeKind, TargetKey
from .hc_rpc import HcRpcClient, HcRpcError
from .hc_protocol import HcTerminal, parse_terminal
from .herdr_client import TargetResolutionError
from .models import HerdrTarget
from .runtime import RuntimeCapabilities

KNOWN_AGENT_APPS = frozenset({"pi", "claude", "codex"})


@dataclass(frozen=True, slots=True)
class HcMutationReceipt:
    observed_turn: int
    result: dict[str, Any]


class HcClient:
    """Present Herdr Core terminals through the bridge's current client API.

    The client connects directly to an existing local Core socket. It never
    invokes a command that can start Core. This compatibility layer implements
    discovery, bounded reads, normal sends, and interrupt while transport-neutral
    orchestration is introduced incrementally.
    """

    capabilities = RuntimeCapabilities(native_interrupt=True)

    def __init__(self, config: AppConfig):
        self.config = config
        self.rpc = HcRpcClient(config.runtime.core)
        self._core_id: str | None = None
        self._core_name: str | None = None

    @property
    def core_id(self) -> str | None:
        return self._core_id

    @property
    def core_name(self) -> str | None:
        return self._core_name

    def observe_core_identity(self, core_id: str, core_name: str) -> None:
        if self._core_id is not None and self._core_id != core_id:
            raise HcRpcError(
                f"Core identity changed from {self._core_id} to {core_id}; "
                "restart HerdRelay after checking the selected session"
            )
        self._core_id = core_id
        self._core_name = core_name

    def address_allowed(self, target: str) -> bool:
        if not self.config.runtime.core.include_connected_machines and "/" in target:
            return False
        exposure = self.config.runtime.core.target_exposure
        if exposure.mode == "all-eligible":
            return True
        if exposure.mode == "none":
            return False
        return target in exposure.addresses

    def project_terminal(self, terminal: HcTerminal) -> HerdrTarget:
        return self._target_from_terminal(terminal, raw={"address": terminal.address})

    def list_targets(self) -> list[HerdrTarget]:
        return [target for target in self.list_panes() if target.agent_name]

    def list_panes(self) -> list[HerdrTarget]:
        """List exposed live Core terminals from one validated snapshot."""
        result = self._call("terminal.list", {"all": False, "labels": {}})
        items = result.get("terminals")
        if not isinstance(items, list):
            raise HcRpcError("terminal.list did not return a terminal array")

        targets: list[HerdrTarget] = []
        for raw in items:
            terminal = parse_terminal(raw)
            if terminal.state != "live":
                continue
            if not self.address_allowed(terminal.address):
                continue
            targets.append(self._target_from_terminal(terminal, raw=raw))
        return targets

    def target_key(self, target: str) -> TargetKey:
        if self._core_id is None:
            self.list_panes()
        if self._core_id is None:
            raise HcRpcError("Core identity is unavailable")
        self._ensure_target_exposed(target)
        return TargetKey.core(controller_core_id=self._core_id, address=target)

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget:
        if key.runtime is not RuntimeKind.HERDR_CORE:
            raise HcRpcError("legacy target identity cannot be used with Herdr Core")
        targets = self.list_panes()
        if self._core_id != key.controller_core_id:
            raise HcRpcError(
                "bound target belongs to a different Core instance; no action was taken"
            )
        for target in targets:
            if target.target == key.address:
                return target
        raise HcRpcError(f"Core terminal is no longer live: {key.address}")

    def read(
        self,
        target: str,
        *,
        lines: int,
        fmt: str | None = None,
        source: str | None = None,
    ) -> str:
        del fmt, source
        self._ensure_target_exposed(target)
        result = self._call(
            "terminal.read",
            {"terminal": target, "lines": lines, "caller": None},
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise HcRpcError("terminal.read did not return text")
        return text

    def send(self, target: str, message: str) -> None:
        self.send_with_receipt(target, message)

    def send_with_receipt(self, target: str, message: str) -> HcMutationReceipt:
        self._ensure_target_exposed(target)
        raw = self._call("terminal.get", {"terminal": target, "caller": None})
        terminal = parse_terminal(raw)
        if terminal.state != "live":
            raise HcRpcError(f"Core terminal is not live: {target}")
        if terminal.status is not None and terminal.status.state.casefold() == "blocked":
            raise HcRpcError(f"Core terminal is blocked: {target}", kind="blocked")
        projected = self._target_from_terminal(terminal, raw=raw)
        if not projected.agent_name:
            raise HcRpcError(f"Core terminal is not an eligible live agent: {target}")
        result = self._call(
            "terminal.send",
            {
                "terminal": target,
                "text": message,
                "caller": None,
                "force": False,
                "wait": False,
            },
        )
        return HcMutationReceipt(observed_turn=terminal.turn, result=result)

    def interrupt(self, target: str) -> dict[str, Any]:
        return self.interrupt_with_receipt(target).result

    def interrupt_with_receipt(self, target: str) -> HcMutationReceipt:
        self._ensure_target_exposed(target)
        raw = self._call("terminal.get", {"terminal": target, "caller": None})
        terminal = parse_terminal(raw)
        projected = self._target_from_terminal(terminal, raw=raw)
        if not projected.agent_name:
            raise HcRpcError(f"Core terminal is not an eligible live agent: {target}")
        state = terminal.status.state.casefold() if terminal.status else "unknown"
        if state not in {"working", "blocked"}:
            raise HcRpcError(
                f"Core terminal is not interruptible in state {state}: {target}"
            )
        result = self._call("terminal.interrupt", {"terminal": target})
        return HcMutationReceipt(observed_turn=terminal.turn, result=result)

    def resolve_target(self, query: str) -> HerdrTarget:
        candidates = []
        for target in self.list_targets():
            values = {
                target.target,
                target.label,
                target.agent_name,
                target.workspace_label,
                target.tab_label,
                target.cwd,
            }
            if query in {value for value in values if value}:
                candidates.append(target)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return HerdrTarget(target=query)
        raise TargetResolutionError(f"Target is ambiguous: {query}")

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        call = self.rpc.call(method, params)
        self.observe_core_identity(call.hello.core.id, call.hello.core.name)
        return call.result

    def _ensure_target_exposed(self, target: str) -> None:
        if not self.config.runtime.core.include_connected_machines and "/" in target:
            raise HcRpcError(f"connected-machine target is disabled: {target}")
        if not self.address_allowed(target):
            raise HcRpcError(
                f"Core terminal is not exposed by target policy: {target}"
            )

    def _target_from_terminal(
        self, terminal: HcTerminal, *, raw: dict[str, Any]
    ) -> HerdrTarget:
        status = terminal.status
        app = status.app.casefold() if status and status.app else None
        eligible = bool(
            terminal.state == "live" and terminal.reported and app in KNOWN_AGENT_APPS
        )
        if self.config.runtime.core.require_app_visible:
            eligible = eligible and bool(
                terminal.labels.get("herdr.space") and terminal.labels.get("herdr.tab")
            )
        if not self.config.runtime.core.include_hidden_workers and terminal.parent:
            eligible = False

        where = terminal.labels.get("where")
        workspace_label, tab_label = _split_where(where)
        cwd = terminal.foreground.cwd if terminal.foreground else terminal.cwd
        return HerdrTarget(
            target=terminal.address,
            kind="terminal",
            label=where or terminal.name or terminal.title or terminal.address,
            agent_name=app if eligible else None,
            status=status.state if status and eligible else None,
            workspace_label=workspace_label,
            tab_label=tab_label,
            cwd=cwd,
            raw=raw,
        )


def _split_where(where: str | None) -> tuple[str | None, str | None]:
    if not where:
        return None, None
    workspace, separator, tab = where.partition(" / ")
    return workspace or None, (tab or None) if separator else None
