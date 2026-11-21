from __future__ import annotations

import json
from pathlib import Path

import pytest

from herdrelay.config import (
    AppConfig,
    HcRuntimeConfig,
    HcTargetExposureConfig,
    RuntimeConfig,
)
from herdrelay.domain import (
    MutationFailedError,
    MutationOutcomeUnknownError,
    MutationRejectedError,
    TargetKey,
)
from herdrelay.hc_client import HcClient
from herdrelay.hc_protocol import parse_hello_result
from herdrelay.hc_rpc import (
    HcCallResult,
    HcMutationOutcomeUnknown,
    HcRpcError,
)
from herdrelay.herdr_client import HerdrClient
from herdrelay.runtime import QuestionAnswerRuntime, RuntimeClient
from herdrelay.runtime_client import build_runtime_client

ROOT = Path(__file__).parents[1]
HELLO = parse_hello_result(
    json.loads((ROOT / "tests/fixtures/hc/hello.json").read_text())
)


def core_config(**core_overrides) -> AppConfig:
    defaults = {
        "target_exposure": HcTargetExposureConfig(
            mode="selected",
            addresses=frozenset(
                {
                    "t1",
                    "t2",
                    "t3",
                    "t5",
                    "t6",
                    "remote/t1",
                    "remote/t4",
                    "blocked",
                    "shell",
                }
            ),
        )
    }
    defaults.update(core_overrides)
    core = HcRuntimeConfig(**defaults)
    return AppConfig(
        discord_token="token",
        runtime=RuntimeConfig(provider="herdr-core", core=core),
    )


def terminal(
    address: str,
    *,
    app: str | None = "pi",
    state: str = "idle",
    labels: dict[str, str] | None = None,
    parent: str | None = None,
    reported: bool = True,
):
    status = {"state": state, "app": app} if app is not None else None
    return {
        "address": address,
        "labels": labels
        if labels is not None
        else {
            "herdr.space": "space",
            "herdr.tab": "tab",
            "where": "project / Agent",
        },
        "argv": ["pi"],
        "cwd": "/tmp/project",
        "state": "live",
        "created_ms": 1,
        "screen": "exact",
        "cols": 100,
        "rows": 30,
        "status": status,
        "status_ms": 2,
        "turn": 3,
        "reported": reported,
        "title": "π - project",
        "parent": parent,
        "foreground": {
            "pid": 123,
            "name": "node" if app else "zsh",
            "argv": [app] if app else ["/bin/zsh"],
            "cwd": "/tmp/project/current",
        },
    }


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.list_payload = {"terminals": [], "hidden_finished": 0, "machines": []}
        self.terminals = {}
        self.sent = []
        self.hello = HELLO

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "terminal.list":
            result = self.list_payload
        elif method == "terminal.get":
            result = self.terminals[params["terminal"]]
        elif method == "terminal.read":
            result = {"text": "screen", "complete": True, "how": "screen"}
        elif method == "terminal.send":
            self.sent.append((params["terminal"], params["text"]))
            result = {"turn": 4, "status": "idle", "started": True}
        elif method == "terminal.interrupt":
            result = {"outcome": "stopped", "key": "esc"}
        else:
            raise AssertionError(f"unexpected method: {method}")
        return HcCallResult(hello=self.hello, result=result)


def make_client(config: AppConfig | None = None) -> tuple[HcClient, FakeRpc]:
    client = HcClient(config or core_config())
    rpc = FakeRpc()
    client.rpc = rpc
    return client, rpc


def test_core_client_lists_only_verified_visible_root_agents():
    client, rpc = make_client()
    rpc.list_payload = {
        "terminals": [
            terminal("t1"),
            terminal("t2", app=None, reported=False),
            terminal("t3", app=None, reported=False),
            terminal("remote/t4"),
            terminal("t5", parent="t1"),
            terminal("t6", labels={"where": "hidden / 1"}),
            {**terminal("t1"), "address": "finished", "state": "finished"},
        ],
        "hidden_finished": 1,
        "machines": [],
    }

    panes = client.list_panes()

    assert [pane.target for pane in panes] == ["t1", "t2", "t3", "t5", "t6"]
    assert [pane.target for pane in panes if pane.agent_name] == ["t1"]
    active = panes[0]
    assert active.agent_name == "pi"
    assert active.status == "idle"
    assert active.workspace_label == "project"
    assert active.tab_label == "Agent"
    assert active.cwd == "/tmp/project/current"
    assert client.core_id == "core-test-001"


def test_core_client_all_eligible_mode_exposes_local_agents():
    client, rpc = make_client(
        core_config(target_exposure=HcTargetExposureConfig())
    )
    rpc.list_payload["terminals"] = [terminal("t1")]

    assert [target.target for target in client.list_targets()] == ["t1"]


def test_core_client_none_mode_exposes_nothing():
    client, rpc = make_client(
        core_config(
            target_exposure=HcTargetExposureConfig(mode="none")
        )
    )
    rpc.list_payload["terminals"] = [terminal("t1")]

    assert client.list_panes() == []


def test_core_client_can_include_hidden_workers_and_unlabeled_agents_by_policy():
    config = core_config(require_app_visible=False, include_hidden_workers=True)
    client, rpc = make_client(config)
    rpc.list_payload["terminals"] = [
        terminal("t5", parent="t1"),
        terminal("t6", labels={"where": "hidden / 1"}),
    ]

    assert [target.target for target in client.list_targets()] == ["t5", "t6"]


def test_core_client_aborts_invalid_list_before_archival_decisions():
    client, rpc = make_client()
    rpc.list_payload = {"terminals": [{"state": "live"}]}

    with pytest.raises(ValueError, match="must be"):
        client.list_panes()


def test_core_client_refuses_core_identity_change():
    client, rpc = make_client()
    client.list_panes()
    changed = json.loads((ROOT / "tests/fixtures/hc/hello.json").read_text())
    changed["core"]["id"] = "another-core"
    rpc.hello = parse_hello_result(changed)

    with pytest.raises(HcRpcError, match="Core identity changed"):
        client.list_panes()


def test_core_client_resolves_only_exact_pinned_target_identity():
    client, rpc = make_client()
    rpc.list_payload["terminals"] = [terminal("t1")]

    key = client.target_key("t1")

    assert key == TargetKey.core(controller_core_id="core-test-001", address="t1")
    assert client.resolve_target_key(key).target == "t1"
    with pytest.raises(HcRpcError, match="different Core"):
        client.resolve_target_key(
            TargetKey.core(controller_core_id="stale-core", address="t1")
        )
    with pytest.raises(HcRpcError, match="no longer live"):
        client.resolve_target_key(
            TargetKey.core(controller_core_id="core-test-001", address="t2")
        )


def test_core_client_reads_sends_and_interrupts_without_raw_keys():
    client, rpc = make_client()
    rpc.terminals["t1"] = terminal("t1")

    assert client.read("t1", lines=20, fmt="text", source="recent") == "screen"
    send_receipt = client.send_with_receipt("t1", "continue")
    assert rpc.sent == [("t1", "continue")]
    assert send_receipt.observed_turn == 3
    assert send_receipt.result["started"] is True
    rpc.terminals["t1"] = terminal("t1", state="working")
    interrupt_receipt = client.interrupt_with_receipt("t1")
    assert interrupt_receipt.observed_turn == 3
    assert interrupt_receipt.result == {"outcome": "stopped", "key": "esc"}

    send_call = next(params for method, params in rpc.calls if method == "terminal.send")
    assert send_call["force"] is False
    assert send_call["wait"] is False
    assert not isinstance(client, QuestionAnswerRuntime)


def test_core_client_refuses_send_to_blocked_stale_or_unlisted_target():
    client, rpc = make_client()
    rpc.terminals["blocked"] = terminal("blocked", state="blocked")
    rpc.terminals["shell"] = terminal("shell", app=None, reported=False)
    rpc.terminals["t1"] = terminal("t1", state="idle")

    with pytest.raises(MutationRejectedError, match="blocked") as blocked:
        client.send("blocked", "yes")
    assert blocked.value.code == "blocked"
    with pytest.raises(MutationRejectedError, match="not an eligible live agent"):
        client.send("shell", "continue")
    with pytest.raises(MutationRejectedError, match="connected-machine target is disabled"):
        client.send("remote/t1", "continue")
    with pytest.raises(HcRpcError, match="not interruptible in state idle"):
        client.interrupt("t1")
    with pytest.raises(MutationRejectedError, match="not exposed by target policy"):
        client.send("t99", "continue")
    assert rpc.sent == []


def test_core_client_normalizes_definitive_and_unknown_send_failures():
    client, rpc = make_client()
    rpc.terminals["t1"] = terminal("t1")
    original_call = rpc.call

    def fail_send(method, params):
        if method == "terminal.send":
            raise HcRpcError("Core rejected terminal.send")
        return original_call(method, params)

    rpc.call = fail_send
    with pytest.raises(MutationFailedError, match="rejected") as failed:
        client.send("t1", "continue")
    assert failed.value.code is None

    def lose_send_response(method, params):
        if method == "terminal.send":
            raise HcMutationOutcomeUnknown("connection closed")
        return original_call(method, params)

    rpc.call = lose_send_response
    with pytest.raises(MutationOutcomeUnknownError, match="connection closed"):
        client.send("t1", "continue")


def test_runtime_client_factory_keeps_legacy_default():
    legacy = build_runtime_client(AppConfig(discord_token="token"))
    core = build_runtime_client(core_config())

    assert isinstance(legacy, HerdrClient)
    assert isinstance(core, HcClient)
    assert isinstance(legacy, RuntimeClient)
    assert isinstance(core, RuntimeClient)
    assert isinstance(legacy, QuestionAnswerRuntime)
    assert not isinstance(core, QuestionAnswerRuntime)
    assert legacy.capabilities.native_interrupt is False
    assert core.capabilities.native_interrupt is True
