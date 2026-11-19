from __future__ import annotations

import pytest

from herdrelay.config import AppConfig, OrcaRuntimeConfig, RuntimeConfig
from herdrelay.domain import BackendKind
from herdrelay.orca_cli import OrcaAgent, OrcaCliError, OrcaTerminal
from herdrelay.orca_client import OrcaClient


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


def terminal(*, connected=True, writable=True):
    raw = {
        "handle": "term-1",
        "incarnationId": "incarnation-1",
        "executionHostId": "local",
        "tabId": "tab",
        "leafId": "pane",
        "worktreeId": "repo::/worktree",
        "connected": connected,
        "writable": writable,
        "agentIdentity": "pi",
    }
    return OrcaTerminal(
        handle="term-1",
        incarnation_id="incarnation-1",
        host_id="local",
        tab_id="tab",
        leaf_id="pane",
        worktree_id="repo::/worktree",
        connected=connected,
        writable=writable,
        agent_identity="pi",
        raw=raw,
    )


def test_orca_client_sends_to_exact_writable_terminal():
    client = make_client()
    calls = []

    class FakeCli:
        def agent_list(self):
            return "runtime-1", [agent()]

        def terminal_list(self):
            return "runtime-1", [terminal()]

        def terminal_send(self, **kwargs):
            calls.append(kwargs)
            return {"accepted": True, "submitted": True, "requestId": "request-1"}

    client.cli = FakeCli()
    [target] = client.list_targets()

    receipt = client.send_with_receipt(target.target, "continue")

    assert receipt.observed_turn is None
    assert receipt.result["requestId"] == "request-1"
    assert calls == [
        {
            "handle": "term-1",
            "text": "continue",
            "wait_submit_seconds": 5,
        }
    ]


def test_orca_client_rejects_changed_runtime_and_ambiguous_terminal():
    client = make_client()
    client.cli = type(
        "FakeCli",
        (),
        {"agent_list": lambda self: ("runtime-1", [agent()])},
    )()
    [target] = client.list_targets()
    key = client.target_key(target.target)
    client.cli = type(
        "ChangedCli",
        (),
        {"agent_list": lambda self: ("runtime-2", [agent()])},
    )()

    with pytest.raises(OrcaCliError, match="different Orca runtime"):
        client.resolve_target_key(key)

    class AmbiguousCli:
        def agent_list(self):
            return "runtime-1", [agent()]

        def terminal_list(self):
            return "runtime-1", [terminal(), terminal()]

    client.cli = AmbiguousCli()
    with pytest.raises(OrcaCliError, match="exactly one"):
        client.send(target.target, "continue")


def test_orca_client_rejects_send_while_agent_is_blocked():
    client = make_client()
    client.cli = type(
        "BlockedCli",
        (),
        {"agent_list": lambda self: ("runtime-1", [agent(state="blocked")])},
    )()

    with pytest.raises(OrcaCliError, match="blocked") as caught:
        client.send("local/tab:pane", "continue")

    assert caught.value.kind == "blocked"
