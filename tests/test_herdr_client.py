from dataclasses import replace
from unittest.mock import Mock

import pytest

from herdrelay.config import (
    AppConfig,
    HerdrCliConfig,
    RuntimeConfig,
    TuiTargetExposureConfig,
)
from herdrelay.domain import (
    BackendUnavailableError,
    MutationFailedError,
    MutationOutcomeUnknownError,
    MutationReceipt,
    TargetKey,
)
from herdrelay.herdr_cli import HerdrCliError
from herdrelay.herdr_client import HerdrClient, normalize_targets
from herdrelay.models import HerdrTarget, PendingQuestion, QuestionOption


def test_tui_target_exposure_filters_exact_addresses():
    config = replace(
        AppConfig(discord_token="token"),
        runtime=RuntimeConfig(
            provider="herdr-tui",
            tui=HerdrCliConfig(
                target_exposure=TuiTargetExposureConfig(
                    mode="selected", addresses=frozenset({"w1:p2"})
                )
            ),
        ),
    )
    client = HerdrClient(config)
    targets = [
        HerdrTarget(target="w1:p1"),
        HerdrTarget(target="w1:p2"),
    ]

    assert client._filter_exposed(targets) == [targets[1]]


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

    receipt = client.send("w7:p2", "hello")

    assert receipt == MutationReceipt(observed_turn=None, result={})
    assert fake_cli.calls == [("agent_prompt", "w7:p2", "hello")]


def test_send_timeout_is_unknown_and_never_falls_back():
    client = HerdrClient(
        AppConfig(discord_token="token", allow_pane_send_fallback=True)
    )
    fake_cli = Mock()
    fake_cli.agent_prompt.side_effect = HerdrCliError(
        "Herdr command timed out: herdr agent prompt"
    )
    client.cli = fake_cli

    with pytest.raises(MutationOutcomeUnknownError, match="timed out"):
        client.send("w7:p2", "hello")

    fake_cli.pane_run.assert_not_called()


def test_send_definitive_cli_failure_uses_normalized_contract():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = Mock()
    fake_cli.agent_prompt.side_effect = HerdrCliError(
        "Herdr command failed: herdr agent prompt"
    )
    client.cli = fake_cli

    with pytest.raises(MutationFailedError, match="command failed") as caught:
        client.send("w7:p2", "hello")

    assert isinstance(caught.value, HerdrCliError)
    fake_cli.pane_run.assert_not_called()


def test_send_missing_cli_uses_backend_unavailable_contract():
    client = HerdrClient(AppConfig(discord_token="token"))
    fake_cli = Mock()
    fake_cli.agent_prompt.side_effect = HerdrCliError(
        "Herdr CLI not found: herdr"
    )
    client.cli = fake_cli

    with pytest.raises(BackendUnavailableError, match="CLI not found") as caught:
        client.send("w7:p2", "hello")

    assert isinstance(caught.value, HerdrCliError)


def test_send_fallback_returns_receipt_after_definitive_agent_prompt_failure():
    client = HerdrClient(
        AppConfig(discord_token="token", allow_pane_send_fallback=True)
    )
    fake_cli = Mock()
    fake_cli.agent_prompt.side_effect = HerdrCliError(
        "Herdr command failed: herdr agent prompt"
    )
    client.cli = fake_cli

    receipt = client.send("w7:p2", "hello")

    assert receipt == MutationReceipt(
        observed_turn=None, result={"fallback": "pane_run"}
    )
    fake_cli.pane_run.assert_called_once_with("w7:p2", "hello")


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

    assert client.list_workspaces() == {"w1": "cbo_mppi", "w7": "herdrelay"}


def test_list_targets_attaches_workspace_labels():
    client = HerdrClient(AppConfig(discord_token="token"))
    client.cli = AgentWithWorkspaceFakeCli()

    targets = {t.target: t for t in client.list_targets()}

    assert targets["w1:p1"].workspace_label == "cbo_mppi"
    assert targets["w7:p3"].workspace_label == "herdrelay"
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


class WorkspaceFakeCli:
    def workspace_list(self):
        return {
            "result": {
                "workspaces": [
                    {"workspace_id": "w1", "label": "cbo_mppi"},
                    {"workspace_id": "w7", "label": "herdrelay"},
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


def test_legacy_client_preserves_raw_pane_route_identity():
    client = HerdrClient(AppConfig(discord_token="token"))
    target = client.target_key("w1:p1")
    client.resolve_target = Mock(
        return_value=HerdrTarget(target="w1:p1", agent_name="pi")
    )

    assert target == TargetKey.legacy("w1:p1")
    assert target.to_route_value() == "w1:p1"
    assert client.resolve_target_key(target).target == "w1:p1"


def test_answer_question_keeps_tui_key_sequence_inside_legacy_backend():
    client = HerdrClient.__new__(HerdrClient)
    client._send_question_keys = Mock()
    question = PendingQuestion(
        pane_id="w1:p1",
        event_id="question-1",
        prompt="Choose",
        options=(QuestionOption("One"), QuestionOption("Two")),
    )

    client.answer_question(question, (1,))

    client._send_question_keys.assert_called_once_with(
        "w1:p1", ("Down", "Enter"), interval_seconds=0.5
    )


def test_question_keys_pause_between_keys(monkeypatch):
    from herdrelay import herdr_client as module

    sent = []
    sleeps = []
    client = module.HerdrClient.__new__(module.HerdrClient)
    client.cli = type("Cli", (), {"pane_send_keys": lambda self, target, *keys: sent.append(keys)})()
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    client._send_question_keys(
        "w1:p1", ("Down", "Space", "Enter"), interval_seconds=0.5
    )

    assert sent == [("Down",), ("Space",), ("Enter",)]
    assert sleeps == [0.5, 0.5]
