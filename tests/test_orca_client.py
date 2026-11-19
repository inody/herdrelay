from __future__ import annotations

import pytest

from herdrelay.config import AppConfig, OrcaRuntimeConfig, RuntimeConfig
from herdrelay.domain import BackendKind
from herdrelay.orca_cli import OrcaAgent, OrcaCliError
from herdrelay.orca_client import OrcaClient, OrcaUnsupportedOperation


def agent(
    *,
    pane_key="tab:pane",
    worktree_id="repo::/worktree",
    host_id="local",
    kind="terminal",
    state="done",
    message="completed response",
):
    raw = {
        "orcaSessionId": "session-1",
        "kind": kind,
        "agent": "pi",
        "state": state,
        "worktreeId": worktree_id,
        "workspace": "main",
        "repo": "project",
        "hostId": host_id,
        "paneKey": pane_key,
        "title": "Pi - project",
        "lastAssistantMessage": message,
        "updatedAt": 123,
    }
    return OrcaAgent(
        session_id="session-1",
        kind=kind,
        agent="pi",
        state=state,
        host_id=host_id,
        worktree_id=worktree_id,
        pane_key=pane_key,
        workspace="main",
        repo="project",
        title="Pi - project",
        last_assistant_message=message,
        updated_at=123,
        raw=raw,
    )


def make_client(*, allowed_panes=(), allowed_worktrees=("repo::/worktree",)):
    config = AppConfig(
        discord_token="",
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(
                allowed_host_ids=frozenset({"local"}),
                allowed_pane_keys=frozenset(allowed_panes),
                allowed_worktree_ids=frozenset(allowed_worktrees),
            ),
        ),
    )
    return OrcaClient(config)


def test_orca_client_lists_allowlisted_targets_and_pins_runtime_identity():
    client = make_client()
    client.cli = type(
        "FakeCli",
        (),
        {"agent_list": lambda self: ("runtime-1", [agent()])},
    )()

    [target] = client.list_targets()
    key = client.target_key(target.target)

    assert target.target == "local/tab:pane"
    assert target.agent_name == "pi"
    assert target.status == "done"
    assert key.backend is BackendKind.ORCA
    assert key.instance_id == "runtime-1"
    assert key.address == "local/tab:pane"
    assert client.resolve_target_key(key) == target


def test_orca_client_filters_hosts_kinds_and_deny_by_default_targets():
    client = make_client(allowed_worktrees=())
    client.cli = type(
        "FakeCli",
        (),
        {
            "agent_list": lambda self: (
                "runtime-1",
                [
                    agent(),
                    agent(host_id="remote"),
                    agent(kind="chat", pane_key=None),
                ],
            )
        },
    )()

    assert client.list_targets() == []


def test_orca_client_reads_bounded_last_assistant_message():
    client = make_client(allowed_panes=("tab:pane",), allowed_worktrees=())
    item = agent(message="one\ntwo\nthree")
    client.cli = type(
        "FakeCli",
        (),
        {"agent_list": lambda self: ("runtime-1", [item])},
    )()
    [target] = client.list_targets()

    assert client.read(target.target, lines=2) == "two\nthree"


def test_orca_client_rejects_changed_runtime_and_mutations():
    client = make_client()
    fake = type(
        "FakeCli",
        (),
        {"agent_list": lambda self: ("runtime-1", [agent()])},
    )()
    client.cli = fake
    [target] = client.list_targets()
    key = client.target_key(target.target)
    client.cli = type(
        "ChangedCli",
        (),
        {"agent_list": lambda self: ("runtime-2", [agent()])},
    )()

    with pytest.raises(OrcaCliError, match="different Orca runtime"):
        client.resolve_target_key(key)
    with pytest.raises(OrcaUnsupportedOperation, match="not implemented"):
        client.send(target.target, "continue")
