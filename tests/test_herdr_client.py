from unittest.mock import Mock

import pytest

from herdr_discord_bridge.config import AppConfig
from herdr_discord_bridge.herdr_cli import HerdrCliError
from herdr_discord_bridge.herdr_client import HerdrClient, normalize_targets


def test_normalize_targets_from_result_agents():
    targets = normalize_targets(
        {
            "ok": True,
            "result": {
                "agents": [
                    {
                        "pane_id": "1-1",
                        "agent_name": "codex",
                        "agent_status": "blocked",
                        "workspace": {"label": "bridge"},
                        "pane": {"foreground_cwd": "/tmp/bridge"},
                    }
                ]
            },
        },
        preferred_kind="agent",
    )

    assert len(targets) == 1
    assert targets[0].target == "1-1"
    assert targets[0].agent_name == "codex"
    assert targets[0].status == "blocked"
    assert targets[0].workspace_label == "bridge"
    assert targets[0].cwd == "/tmp/bridge"


def test_normalize_targets_from_plain_pane_list():
    targets = normalize_targets(
        [
            {
                "pane_id": "1-2",
                "label": "logs",
                "agent_status": "idle",
            }
        ],
        preferred_kind="pane",
    )

    assert targets[0].target == "1-2"
    assert targets[0].label == "logs"
    assert targets[0].kind == "pane"


def test_read_uses_pane_read_without_agent_lifecycle_dependency():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = ReadFakeCli()
    client.cli = fake_cli

    assert client.read("w7:p2", lines=1000, fmt="text", source="recent") == "output"
    assert fake_cli.calls == [("pane_read", "w7:p2", 1000, "text", "recent")]


def test_send_uses_agent_prompt_without_extra_enter():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = FakeCli()
    client.cli = fake_cli

    client.send("w7:p2", "hello")

    assert fake_cli.calls == [("agent_prompt", "w7:p2", "hello")]


def test_agent_start_splits_source_pane_then_uses_current_cli_shape():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = StartFakeCli()
    client.cli = fake_cli

    started = client.agent_start(
        "claude",
        cwd="/tmp/project",
        argv=["claude", "--model", "sonnet"],
        split="down",
        source_pane="w7:p1",
    )

    assert started.pane_id == "w7:p2"
    assert fake_cli.calls == [
        ("pane_split", "w7:p1", "down", "/tmp/project"),
        ("agent_start", "claude", "claude", "w7:p2", ["--model", "sonnet"]),
    ]


def test_normalize_targets_resolves_workspace_label_from_map():
    targets = normalize_targets(
        {"result": {"agents": [
            {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle", "workspace_id": "w1"}
        ]}},
        preferred_kind="agent",
        workspace_labels={"w1": "cbo_mppi"},
    )

    assert targets[0].workspace_label == "cbo_mppi"


def test_normalize_targets_workspace_label_falls_back_to_nested():
    targets = normalize_targets(
        {"result": {"agents": [
            {"pane_id": "1", "agent": "codex", "workspace": {"label": "nested"}}
        ]}},
        preferred_kind="agent",
    )

    assert targets[0].workspace_label == "nested"


def test_list_workspaces_maps_ids_to_labels():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = WorkspaceFakeCli()

    assert client.list_workspaces() == {"w1": "cbo_mppi", "w7": "herdr-chat-bridge"}


def test_list_targets_attaches_workspace_labels():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = AgentWithWorkspaceFakeCli()

    targets = {t.target: t for t in client.list_targets()}

    assert targets["w1:p1"].workspace_label == "cbo_mppi"
    assert targets["w7:p3"].workspace_label == "herdr-chat-bridge"
    assert targets["w7:p3"].status == "blocked"


def test_list_panes_includes_shells_without_querying_agent_list():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = Mock()
    client.cli.workspace_list.return_value = {
        "result": {"workspaces": [{"workspace_id": "w1", "label": "project"}]}
    }
    client.cli.pane_list.return_value = {"result": {"panes": [
        {"pane_id": "w1:p1", "workspace_id": "w1", "agent": None, "agent_status": "unknown"},
        {"pane_id": "w1:p2", "workspace_id": "w1", "agent": "pi", "agent_status": "idle"},
    ]}}

    targets = client.list_panes()

    assert [target.target for target in targets] == ["w1:p1", "w1:p2"]
    assert targets[0].agent_name is None
    assert targets[0].workspace_label == "project"
    assert targets[1].agent_name == "pi"
    client.cli.agent_list.assert_not_called()


def test_list_panes_accepts_successful_empty_snapshot():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = Mock()
    client.cli.pane_list.return_value = {"result": {"panes": []}}
    client.cli.workspace_list.return_value = {"result": {"workspaces": []}}

    assert client.list_panes() == []


@pytest.mark.parametrize("payload", [
    {}, {"result": {}}, {"result": {"agents": []}},
    {"result": {"panes": None}}, {"error": {"code": "server_not_running"}},
    {"result": {"panes": [{"agent": "pi"}]}},
    {"result": {"panes": [None]}}, {"result": {"panes": [{"pane_id": " "}]}},
])
def test_list_panes_rejects_invalid_snapshot_instead_of_reporting_all_panes_gone(payload):
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = Mock()
    client.cli.pane_list.return_value = payload

    with pytest.raises(HerdrCliError, match="valid pane list"):
        client.list_panes()


def test_list_panes_propagates_cli_failure():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = Mock()
    client.cli.pane_list.side_effect = HerdrCliError("socket unavailable")

    with pytest.raises(HerdrCliError, match="socket unavailable"):
        client.list_panes()


class ReadFakeCli:
    def __init__(self):
        self.calls = []

    def pane_read(self, target, *, lines, fmt, source):
        self.calls.append(("pane_read", target, lines, fmt, source))
        return "output"


class FakeCli:
    def __init__(self):
        self.calls = []

    def agent_prompt(self, target, message):
        self.calls.append(("agent_prompt", target, message))

    def pane_send_keys(self, target, *keys):
        self.calls.append(("pane_send_keys", target, keys))


class StartFakeCli:
    def __init__(self):
        self.calls = []

    def pane_split(self, pane_id, *, direction, cwd):
        self.calls.append(("pane_split", pane_id, direction, cwd))
        return {"result": {"pane": {"pane_id": "w7:p2"}}}

    def agent_start(self, name, *, kind, pane_id, argv):
        self.calls.append(("agent_start", name, kind, pane_id, argv))
        return {"result": {"agent": {"pane_id": pane_id}}}


class WorkspaceFakeCli:
    def workspace_list(self):
        return {
            "result": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "cbo_mppi"},
                    {"workspace_id": "w7", "label": "herdr-chat-bridge"},
                ]
            }
        }


class AgentWithWorkspaceFakeCli(WorkspaceFakeCli):
    def agent_list(self):
        return {
            "result": {
                "agents": [
                    {"pane_id": "w1:p1", "agent": "codex", "agent_status": "idle", "workspace_id": "w1"},
                    {"pane_id": "w7:p3", "agent": "claude", "agent_status": "blocked", "workspace_id": "w7"},
                ]
            }
        }
