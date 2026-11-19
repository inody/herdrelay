from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from herdrelay.config import OrcaRuntimeConfig
from herdrelay.orca_cli import OrcaCli, OrcaCliError


def completed(payload, *, returncode=0, stderr=""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def test_orca_status_uses_documented_json_cli(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(
            {
                "ok": True,
                "result": {
                    "runtime": {
                        "runtimeId": "runtime-1",
                        "reachable": True,
                        "state": "ready",
                        "appVersion": "1.4.263",
                    }
                },
                "_meta": {"runtimeId": "runtime-1"},
            }
        )

    monkeypatch.setattr("herdrelay.orca_cli.subprocess.run", fake_run)
    cli = OrcaCli(
        OrcaRuntimeConfig(
            cli_path="/opt/orca",
            environment="local",
            command_timeout_seconds=7,
        )
    )

    status = cli.status()

    assert status.runtime_id == "runtime-1"
    assert status.reachable is True
    assert status.state == "ready"
    assert calls[0][0] == [
        "/opt/orca",
        "status",
        "--environment",
        "local",
        "--json",
    ]
    assert calls[0][1]["timeout"] == 7


def test_orca_agent_list_parses_stable_identity_and_output(monkeypatch):
    def fake_run(command, **kwargs):
        return completed(
            {
                "ok": True,
                "result": {
                    "agents": [
                        {
                            "orcaSessionId": "session-1",
                            "kind": "terminal",
                            "agent": "pi",
                            "state": "done",
                            "worktreeId": "repo::/worktree",
                            "workspace": "main",
                            "repo": "project",
                            "hostId": "local",
                            "paneKey": "tab:pane",
                            "title": "Pi - project",
                            "lastAssistantMessage": "finished",
                            "updatedAt": 123,
                        }
                    ],
                    "truncated": False,
                },
                "_meta": {"runtimeId": "runtime-1"},
            }
        )

    monkeypatch.setattr("herdrelay.orca_cli.subprocess.run", fake_run)

    runtime_id, agents = OrcaCli(OrcaRuntimeConfig()).agent_list()

    assert runtime_id == "runtime-1"
    [agent] = agents
    assert agent.session_id == "session-1"
    assert agent.pane_key == "tab:pane"
    assert agent.last_assistant_message == "finished"
    assert agent.updated_at == 123


def test_orca_cli_preserves_structured_error(monkeypatch):
    monkeypatch.setattr(
        "herdrelay.orca_cli.subprocess.run",
        lambda command, **kwargs: completed(
            {
                "ok": False,
                "error": {"code": "runtime_unavailable", "message": "offline"},
            },
            returncode=1,
        ),
    )

    with pytest.raises(OrcaCliError, match="offline") as caught:
        OrcaCli(OrcaRuntimeConfig()).agent_list()

    assert caught.value.kind == "runtime_unavailable"


def test_orca_cli_rejects_malformed_agent_payload(monkeypatch):
    monkeypatch.setattr(
        "herdrelay.orca_cli.subprocess.run",
        lambda command, **kwargs: completed(
            {
                "ok": True,
                "result": {"agents": [{"kind": "terminal"}]},
                "_meta": {"runtimeId": "runtime-1"},
            }
        ),
    )

    with pytest.raises(OrcaCliError, match="session ID"):
        OrcaCli(OrcaRuntimeConfig()).agent_list()
