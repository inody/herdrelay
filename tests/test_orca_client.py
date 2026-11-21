from __future__ import annotations

import pytest

from herdrelay.config import (
    AppConfig,
    OrcaRuntimeConfig,
    OrcaTargetExposureConfig,
    RuntimeConfig,
)
from herdrelay.domain import (
    BackendKind,
    BackendUnavailableError,
    MutationFailedError,
    MutationOutcomeUnknownError,
    MutationRejectedError,
    TargetKey,
)
from herdrelay.orca_cli import (
    OrcaAgent,
    OrcaCliError,
    OrcaMutationOutcomeUnknown,
    OrcaTerminal,
)
from herdrelay.orca_client import OrcaClient


def agent(
    *,
    pane_key="tab:pane",
    worktree_id="repo::/worktree",
    host_id="local",
    kind="terminal",
    state="done",
    message="completed response",
    workspace="main",
    repo="project",
):
    raw = {
        "orcaSessionId": "session-1",
        "kind": kind,
        "agent": "pi",
        "state": state,
        "worktreeId": worktree_id,
        "workspace": workspace,
        "repo": repo,
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
        workspace=workspace,
        repo=repo,
        title="Pi - project",
        last_assistant_message=message,
        updated_at=123,
        raw=raw,
    )


def make_client(
    *, mode="all-eligible", pane_keys=(), worktree_ids=()
):
    config = AppConfig(
        discord_token="",
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(
                host_ids=frozenset({"local"}),
                target_exposure=OrcaTargetExposureConfig(
                    mode=mode,
                    pane_keys=frozenset(pane_keys),
                    worktree_ids=frozenset(worktree_ids),
                ),
            ),
        ),
    )
    return OrcaClient(config)


def test_orca_client_lists_all_eligible_targets_and_pins_runtime_identity():
    client = make_client()
    client.cli = discovery_cli([agent()])

    [target] = client.list_targets()
    key = client.target_key(target.target)

    assert target.target == "local/tab:pane"
    assert target.agent_name == "pi"
    assert target.status == "done"
    assert key.backend is BackendKind.ORCA
    assert key.instance_id == "runtime-1"
    assert key.address == "local/tab:pane"
    assert client.resolve_target_key(key) == target


def test_orca_client_uses_project_labels_and_worktree_paths():
    client = make_client()
    item = agent(
        repo="herdr-chat-bridge",
        workspace="main",
        worktree_id="repo-id::/Users/example/herdr-chat-bridge",
    )
    client.cli = discovery_cli([item])

    [target] = client.list_targets()

    assert target.workspace_label == "herdr-chat-bridge"
    assert target.cwd == "/Users/example/herdr-chat-bridge"


def test_orca_client_filters_hosts_and_unsupported_kinds():
    client = make_client()
    client.cli = discovery_cli(
        [
            agent(),
            agent(host_id="remote"),
            agent(kind="chat", pane_key=None),
        ]
    )

    assert [target.target for target in client.list_targets()] == [
        "local/tab:pane"
    ]


def test_orca_client_selected_and_none_exposure_modes():
    selected = make_client(
        mode="selected", worktree_ids=("repo::/worktree",)
    )
    selected.cli = discovery_cli([agent()])
    none = make_client(mode="none")
    none.cli = selected.cli

    assert [target.target for target in selected.list_targets()] == [
        "local/tab:pane"
    ]
    assert none.list_targets() == []


def test_orca_client_reads_bounded_last_assistant_message():
    client = make_client(mode="selected", pane_keys=("tab:pane",))
    item = agent(message="one\ntwo\nthree")
    client.cli = discovery_cli([item])
    [target] = client.list_targets()

    assert client.read(target.target, lines=2) == "two\nthree"


def terminal(
    *,
    connected=True,
    writable=True,
    handle="term-1",
    tab_id="tab",
    leaf_id="pane",
    worktree_id="repo::/worktree",
    agent_identity="pi",
):
    raw = {
        "handle": handle,
        "incarnationId": "incarnation-1",
        "executionHostId": "local",
        "tabId": tab_id,
        "leafId": leaf_id,
        "worktreeId": worktree_id,
        "connected": connected,
        "writable": writable,
        "agentIdentity": agent_identity,
    }
    return OrcaTerminal(
        handle=handle,
        incarnation_id="incarnation-1",
        host_id="local",
        tab_id=tab_id,
        leaf_id=leaf_id,
        worktree_id=worktree_id,
        connected=connected,
        writable=writable,
        agent_identity=agent_identity,
        raw=raw,
    )


def discovery_cli(
    agents, *, terminals=None, runtime_id="runtime-1"
):
    terminal_items = [terminal()] if terminals is None else terminals

    class DiscoveryCli:
        def agent_list(self):
            return runtime_id, agents

        def terminal_list(self):
            return runtime_id, terminal_items

    return DiscoveryCli()


def test_orca_client_discovers_starting_agent_from_terminal_topology():
    client = make_client()
    client.cli = discovery_cli([], terminals=[terminal()])

    [target] = client.list_targets()

    assert target.target == "local/tab:pane"
    assert target.agent_name == "pi"
    assert target.status == "starting"


def test_orca_client_keeps_terminal_during_connection_capability_changes():
    client = make_client()
    client.cli = discovery_cli(
        [], terminals=[terminal(connected=False, writable=False)]
    )

    [target] = client.list_targets()

    assert target.target == "local/tab:pane"
    assert target.status == "starting"


def test_orca_client_maps_temporary_pty_topology_to_stable_agent_identity():
    client = make_client()
    pty_key = "pty:repo::/worktree@@1234"
    client.cli = discovery_cli(
        [agent(state="done")],
        terminals=[terminal(tab_id=pty_key, leaf_id=pty_key)],
    )

    [target] = client.list_targets()

    assert target.target == "local/tab:pane"
    assert target.status == "done"


@pytest.mark.parametrize("state", ["working", "blocked", "waiting"])
def test_orca_client_keeps_active_agent_during_topology_gap(state):
    client = make_client()
    client.cli = discovery_cli([agent(state=state)], terminals=[])

    [target] = client.list_targets()

    assert target.target == "local/tab:pane"
    assert target.status == state


def test_orca_client_omits_done_agent_after_terminal_disappears():
    client = make_client()
    client.cli = discovery_cli([agent(state="done")], terminals=[])

    assert client.list_targets() == []


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


def test_orca_client_sends_through_temporary_pty_topology_alias():
    client = make_client()
    calls = []
    pty_key = "pty:repo::/worktree@@1234"

    class FakeCli:
        def agent_list(self):
            return "runtime-1", [agent()]

        def terminal_list(self):
            return "runtime-1", [terminal(tab_id=pty_key, leaf_id=pty_key)]

        def terminal_send(self, **kwargs):
            calls.append(kwargs)
            return {"accepted": True, "submitted": True, "requestId": "request-1"}

    client.cli = FakeCli()

    receipt = client.send_with_receipt("local/tab:pane", "continue")

    assert receipt.result["requestId"] == "request-1"
    assert calls[0]["handle"] == "term-1"


def test_orca_client_retries_transient_discovery_gap_when_resolving_key():
    client = make_client()
    terminal_snapshots = [[], [terminal()]]

    class FlakyDiscoveryCli:
        def agent_list(self):
            return "runtime-1", [agent(state="done")]

        def terminal_list(self):
            return "runtime-1", terminal_snapshots.pop(0)

    client.cli = FlakyDiscoveryCli()
    key = TargetKey.orca(instance_id="runtime-1", address="local/tab:pane")

    target = client.resolve_target_key(key)

    assert target.target == "local/tab:pane"
    assert target.status == "done"


def test_orca_client_retries_transient_discovery_gap_before_send():
    client = make_client()
    terminal_snapshots = [[], [terminal()], [terminal()]]
    calls = []

    class FlakyDiscoveryCli:
        def agent_list(self):
            return "runtime-1", [agent(state="done")]

        def terminal_list(self):
            return "runtime-1", terminal_snapshots.pop(0)

        def terminal_send(self, **kwargs):
            calls.append(kwargs)
            return {"accepted": True, "submitted": True, "requestId": "request-1"}

    client.cli = FlakyDiscoveryCli()

    receipt = client.send_with_receipt("local/tab:pane", "continue")

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
    client.cli = discovery_cli([agent()])
    [target] = client.list_targets()
    key = client.target_key(target.target)
    client.cli = discovery_cli([agent()], runtime_id="runtime-2")

    with pytest.raises(OrcaCliError, match="different Orca runtime"):
        client.resolve_target_key(key)

    class AmbiguousCli:
        def agent_list(self):
            return "runtime-1", [agent()]

        def terminal_list(self):
            return "runtime-1", [terminal(), terminal()]

    client.cli = AmbiguousCli()
    with pytest.raises(BackendUnavailableError, match="exactly one") as caught:
        client.send(target.target, "continue")
    assert isinstance(caught.value, OrcaCliError)


def test_orca_client_rejects_ambiguous_temporary_pty_alias():
    client = make_client()
    first = "pty:repo::/worktree@@1234"
    second = "pty:repo::/worktree@@5678"
    client.cli = discovery_cli(
        [agent(state="working")],
        terminals=[
            terminal(handle="term-1", tab_id=first, leaf_id=first),
            terminal(handle="term-2", tab_id=second, leaf_id=second),
        ],
    )

    targets = client.list_targets()

    assert {target.target for target in targets} == {
        "local/tab:pane",
        f"local/{first}:{first}",
        f"local/{second}:{second}",
    }
    with pytest.raises(BackendUnavailableError, match="exactly one") as caught:
        client.send("local/tab:pane", "continue")
    assert isinstance(caught.value, OrcaCliError)


def test_orca_client_normalizes_definitive_and_unknown_send_failures():
    client = make_client()

    class FailingCli:
        def agent_list(self):
            return "runtime-1", [agent()]

        def terminal_list(self):
            return "runtime-1", [terminal()]

        def terminal_send(self, **kwargs):
            raise OrcaCliError("terminal send failed")

    client.cli = FailingCli()
    with pytest.raises(MutationFailedError, match="send failed") as failed:
        client.send("local/tab:pane", "continue")
    assert isinstance(failed.value, OrcaCliError)

    def lose_response(**kwargs):
        raise OrcaMutationOutcomeUnknown("response timed out")

    client.cli.terminal_send = lose_response
    with pytest.raises(MutationOutcomeUnknownError, match="timed out"):
        client.send("local/tab:pane", "continue")


def test_orca_client_rejects_send_while_agent_is_blocked():
    client = make_client()
    client.cli = discovery_cli([agent(state="blocked")])

    with pytest.raises(MutationRejectedError, match="blocked") as caught:
        client.send("local/tab:pane", "continue")

    assert isinstance(caught.value, OrcaCliError)
    assert caught.value.kind == "blocked"
