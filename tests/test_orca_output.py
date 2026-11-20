from __future__ import annotations

import json
from dataclasses import replace

import pytest

from herdrelay.backends.orca_output import OrcaOutputSource
from herdrelay.config import (
    AppConfig,
    OrcaRuntimeConfig,
    RuntimeConfig,
    StreamConfig,
)
from herdrelay.domain import TargetKey
from herdrelay.models import HerdrTarget
from herdrelay.output_events import load_hook_output_event
from herdrelay.store import Store


class FakeOrcaClient:
    def __init__(self, target):
        self.target = target

    def list_targets(self):
        return [self.target]

    def target_key(self, target):
        assert target == self.target.target
        return TargetKey.orca(instance_id="runtime-1", address=target)


def orca_target(
    *, state: str, message: str, updated_at: int, agent: str = "pi"
) -> HerdrTarget:
    return HerdrTarget(
        target="local/tab:pane",
        kind="orca-terminal",
        label="project",
        agent_name=agent,
        status=state,
        raw={
            "orca_agent": {
                "orcaSessionId": "session-1",
                "state": state,
                "lastAssistantMessage": message,
                "updatedAt": updated_at,
            }
        },
    )


@pytest.mark.asyncio
async def test_orca_output_source_emits_only_new_completed_responses(tmp_path):
    inbox = tmp_path / "inbox"
    config = AppConfig(
        discord_token="",
        database_path=str(tmp_path / "store.sqlite3"),
        enable_streaming=True,
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(),
        ),
        streaming=StreamConfig(hook_inbox_path=str(inbox)),
    )
    client = FakeOrcaClient(
        orca_target(state="working", message="previous", updated_at=1)
    )
    source = OrcaOutputSource(
        config=config,
        store=Store(config.database_path),
        client=client,
    )

    await source.poll_once()
    assert list(inbox.glob("*.json")) == []

    client.target = orca_target(
        state="done", message="completed response", updated_at=2
    )
    await source.poll_once()

    [path] = list(inbox.glob("*.json"))
    event = load_hook_output_event(path, max_bytes=2_000_000)
    assert event.version == 3
    assert event.target == TargetKey.orca(
        instance_id="runtime-1", address="local/tab:pane"
    )
    assert event.generation == "session-1"
    assert event.text == "completed response"

    await source.poll_once()
    assert list(inbox.glob("*.json")) == [path]


@pytest.mark.asyncio
async def test_orca_output_source_skips_claude_completed_responses(tmp_path):
    inbox = tmp_path / "inbox"
    config = AppConfig(
        discord_token="",
        database_path=str(tmp_path / "store.sqlite3"),
        enable_streaming=True,
        runtime=RuntimeConfig(provider="orca"),
        streaming=StreamConfig(hook_inbox_path=str(inbox)),
    )
    client = FakeOrcaClient(
        orca_target(
            state="working",
            message="",
            updated_at=1,
            agent="claude",
        )
    )
    source = OrcaOutputSource(
        config=config,
        store=Store(config.database_path),
        client=client,
    )

    await source.poll_once()
    client.target = orca_target(
        state="done",
        message="completed response",
        updated_at=2,
        agent="claude",
    )
    await source.poll_once()

    assert list(inbox.glob("*.json")) == []


@pytest.mark.asyncio
async def test_orca_output_source_does_not_backfill_existing_done_agent(tmp_path):
    inbox = tmp_path / "inbox"
    config = AppConfig(
        discord_token="",
        database_path=str(tmp_path / "store.sqlite3"),
        enable_streaming=True,
        runtime=RuntimeConfig(provider="orca"),
        streaming=StreamConfig(hook_inbox_path=str(inbox)),
    )
    target = orca_target(state="done", message="old response", updated_at=1)
    client = FakeOrcaClient(target)
    store = Store(config.database_path)
    source = OrcaOutputSource(config=config, store=store, client=client)

    await source.poll_once()
    assert list(inbox.glob("*.json")) == []

    client.target = replace(
        target,
        raw={
            "orca_agent": {
                "orcaSessionId": "session-2",
                "state": "done",
                "lastAssistantMessage": "new response",
                "updatedAt": 2,
            }
        },
    )
    await source.poll_once()

    [path] = list(inbox.glob("*.json"))
    payload = json.loads(path.read_text())
    assert payload["generation"] == "session-2"
    assert payload["text"] == "new response"
