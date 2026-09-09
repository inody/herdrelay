from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from herdr_discord_bridge.config import AppConfig
from herdr_discord_bridge.herdr_cli import HerdrCliError
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.threads import ThreadManager, thread_name


def test_thread_name_marks_blocked_panes():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="cbo_mppi",
        agent_name="codex",
        status="blocked",
    )

    assert thread_name(target) == "🔴 cbo_mppi/codex"


def test_thread_name_has_no_marker_when_not_blocked():
    blocked = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="blocked"
    )
    working = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="working"
    )
    idle = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="idle"
    )

    assert thread_name(blocked).startswith("🔴")
    assert not thread_name(working).startswith("🔴")
    assert not thread_name(idle).startswith("🔴")
    assert thread_name(working) == "herdr-chat-bridge/claude"


def test_thread_name_truncates_to_discord_limit():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="a" * 200,
        agent_name="codex",
        status="idle",
    )

    assert len(thread_name(target)) <= 100


@pytest.fixture
def manager(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_agent_thread(pane_id="w1:p1", thread_id=123, guild_id=1, alias="project")
    store.upsert_binding(
        guild_id=1, channel_id=10, thread_id=123,
        herdr_target="w1:p1", label="project", created_by=1,
    )
    client = Mock()
    parent = SimpleNamespace(id=10, guild=SimpleNamespace(id=1), create_thread=AsyncMock())
    thread = SimpleNamespace(id=123, name="project/pi", archived=False, edit=AsyncMock())
    result = ThreadManager(
        bot=SimpleNamespace(user=None), config=AppConfig(discord_token="token"),
        store=store, client=client,
    )
    result._parent_channel = AsyncMock(return_value=parent)
    result._fetch_thread = AsyncMock(return_value=thread)
    return result


@pytest.mark.asyncio
async def test_existing_thread_survives_no_agents_without_rename_or_binding_change(manager):
    manager.client.list_panes.return_value = [HerdrTarget(target="w1:p1", kind="pane")]
    before = manager.store.find_binding_for_target("w1:p1")

    await manager.sync()

    manager._fetch_thread.return_value.edit.assert_not_awaited()
    manager._parent_channel.return_value.create_thread.assert_not_awaited()
    assert manager.store.find_binding_for_target("w1:p1") == before
    manager.client.list_targets.assert_not_called()


@pytest.mark.asyncio
async def test_shell_without_existing_thread_does_not_create_one(manager):
    manager.client.list_panes.return_value = [
        HerdrTarget(target="w1:p1"), HerdrTarget(target="w1:p2"),
    ]

    await manager.sync()

    manager._parent_channel.return_value.create_thread.assert_not_awaited()
    manager._fetch_thread.return_value.edit.assert_not_awaited()
    assert manager.store.get_agent_thread("w1:p2") is None


@pytest.mark.asyncio
async def test_partial_agent_loss_preserves_shell_thread(manager):
    manager.client.list_panes.return_value = [
        HerdrTarget(target="w1:p1"),
        HerdrTarget(target="w1:p2", agent_name="claude", workspace_label="other"),
    ]
    manager._parent_channel.return_value.create_thread.return_value = SimpleNamespace(id=456)

    await manager.sync()

    manager._fetch_thread.return_value.edit.assert_not_awaited()
    manager._parent_channel.return_value.create_thread.assert_awaited_once()
    assert manager.store.get_agent_thread("w1:p1") == "123"
    assert manager.store.get_agent_thread("w1:p2") == "456"


@pytest.mark.asyncio
async def test_reappearing_agent_reuses_existing_thread(manager):
    manager.client.list_panes.return_value = [
        HerdrTarget(target="w1:p1", workspace_label="project", agent_name="claude"),
    ]

    await manager.sync()

    manager._fetch_thread.return_value.edit.assert_awaited_once_with(name="project/claude")
    manager._parent_channel.return_value.create_thread.assert_not_awaited()
    assert manager.store.get_agent_thread("w1:p1") == "123"


@pytest.mark.asyncio
async def test_shell_preserves_previously_archived_thread_state(manager):
    manager.client.list_panes.return_value = [HerdrTarget(target="w1:p1")]
    manager._fetch_thread.return_value.archived = True

    await manager.sync()

    manager._fetch_thread.assert_not_awaited()
    manager._fetch_thread.return_value.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_relaunch_reactivates_existing_archived_thread(manager):
    manager.client.list_panes.return_value = [
        HerdrTarget(target="w1:p1", workspace_label="project", agent_name="pi"),
    ]
    manager._fetch_thread.return_value.archived = True

    await manager.sync()

    manager._fetch_thread.return_value.edit.assert_awaited_once_with(
        archived=False, reason="Pane w1:p1 active again",
    )
    manager._parent_channel.return_value.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_actually_closed_pane_thread_is_archived(manager):
    manager.client.list_panes.return_value = []

    await manager.sync()

    manager._fetch_thread.return_value.edit.assert_awaited_once_with(
        archived=True, reason="Pane w1:p1 closed",
    )


@pytest.mark.asyncio
async def test_failed_pane_snapshot_never_mutates_threads(manager):
    manager.client.list_panes.side_effect = HerdrCliError("socket unavailable")

    with pytest.raises(HerdrCliError, match="socket unavailable"):
        await manager.sync()

    manager._fetch_thread.assert_not_awaited()
    manager._parent_channel.return_value.create_thread.assert_not_awaited()
