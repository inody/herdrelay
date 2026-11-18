from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import AppConfig
from .hc_rpc import HcRpcClient, HcRpcError
from .hc_protocol import HcTerminal, parse_terminal
from .herdr_client import TargetResolutionError
from .models import HerdrTarget, StartedAgent
from .runtime import RuntimeCapabilities

KNOWN_AGENT_APPS = frozenset({"pi", "claude", "codex"})


class HcUnsupportedOperation(HcRpcError):
    pass


class HcClient:
    """Present Herdr Core terminals through the bridge's current client API.

    The client connects directly to an existing local Core socket. It never
    invokes a command that can start Core. This compatibility layer implements
    discovery, bounded reads, normal sends, and interrupt while transport-neutral
    orchestration is introduced incrementally.
    """

    capabilities = RuntimeCapabilities(native_events=True, native_interrupt=True)

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
        return target in self.config.runtime.core.allowed_terminal_addresses and (
            self.config.runtime.core.include_connected_machines or "/" not in target
        )

    def project_terminal(self, terminal: HcTerminal) -> HerdrTarget:
        return self._target_from_terminal(terminal, raw={"address": terminal.address})

    def list_targets(self) -> list[HerdrTarget]:
        return [target for target in self.list_panes() if target.agent_name]

    def list_panes(self) -> list[HerdrTarget]:
        """List allowlisted live Core terminals from one validated snapshot."""
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

    def read(
        self,
        target: str,
        *,
        lines: int,
        fmt: str | None = None,
        source: str | None = None,
    ) -> str:
        del fmt, source
        self._ensure_target_allowed(target)
        result = self._call(
            "terminal.read",
            {"terminal": target, "lines": lines, "caller": None},
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise HcRpcError("terminal.read did not return text")
        return text

    def send(self, target: str, message: str) -> None:
        self._ensure_target_allowed(target)
        raw = self._call("terminal.get", {"terminal": target, "caller": None})
        terminal = parse_terminal(raw)
        if terminal.state != "live":
            raise HcRpcError(f"Core terminal is not live: {target}")
        if terminal.status is not None and terminal.status.state.casefold() == "blocked":
            raise HcRpcError(f"Core terminal is blocked: {target}", kind="blocked")
        projected = self._target_from_terminal(terminal, raw=raw)
        if not projected.agent_name:
            raise HcRpcError(f"Core terminal is not an eligible live agent: {target}")
        self._call(
            "terminal.send",
            {
                "terminal": target,
                "text": message,
                "caller": None,
                "force": False,
                "wait": False,
            },
        )

    def interrupt(self, target: str) -> dict[str, Any]:
        self._ensure_target_allowed(target)
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
        return self._call("terminal.interrupt", {"terminal": target})

    def send_keys(
        self, target: str, keys: Iterable[str], *, interval_seconds: float = 0
    ) -> None:
        del target, keys, interval_seconds
        raise HcUnsupportedOperation(
            "raw key input is disabled for the Herdr Core backend"
        )

    def send_raw_text(self, target: str, text: str) -> None:
        del target, text
        raise HcUnsupportedOperation(
            "raw text input is disabled for the Herdr Core backend"
        )

    def send_text_enter(self, target: str, text: str) -> None:
        del target, text
        raise HcUnsupportedOperation(
            "approval text injection is disabled for the Herdr Core backend"
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
        raise HcUnsupportedOperation(
            "starting visible agents from chat is not implemented for Herdr Core"
        )

    def pane_close(self, pane_id: str) -> None:
        del pane_id
        raise HcUnsupportedOperation(
            "closing Core terminals from chat is not implemented"
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

    def _ensure_target_allowed(self, target: str) -> None:
        if target not in self.config.runtime.core.allowed_terminal_addresses:
            raise HcRpcError(f"Core terminal is not allowlisted: {target}")
        if not self.address_allowed(target):
            raise HcRpcError(f"connected-machine target is disabled: {target}")

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
