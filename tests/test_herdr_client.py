from herdr_discord_bridge.config import AppConfig
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


def test_send_submits_after_agent_send_by_default():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = FakeCli()
    client.cli = fake_cli

    client.send("w7:p2", "hello")

    assert fake_cli.calls == [
        ("agent_send", "w7:p2", "hello"),
        ("pane_send_keys", "w7:p2", ("Enter",)),
    ]


def test_send_can_skip_submit_after_agent_send():
    client = HerdrClient(AppConfig(discord_token="token", submit_after_agent_send=False))
    fake_cli = FakeCli()
    client.cli = fake_cli

    client.send("w7:p2", "hello")

    assert fake_cli.calls == [("agent_send", "w7:p2", "hello")]


def test_send_honors_zero_submit_delay():
    client = HerdrClient(
        AppConfig(
            discord_token="token",
            submit_after_agent_send=True,
            submit_after_agent_send_delay_seconds=0,
        )
    )
    fake_cli = FakeCli()
    client.cli = fake_cli

    client.send("w7:p2", "hello")

    assert fake_cli.calls == [
        ("agent_send", "w7:p2", "hello"),
        ("pane_send_keys", "w7:p2", ("Enter",)),
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


class FakeCli:
    def __init__(self):
        self.calls = []

    def agent_send(self, target, message):
        self.calls.append(("agent_send", target, message))

    def pane_send_keys(self, target, *keys):
        self.calls.append(("pane_send_keys", target, keys))


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
