from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from herdrelay.config import AppConfig
from herdrelay.domain import BackendKind, TargetKey
from herdrelay.herdr_cli import HerdrCliError
from herdrelay.models import HerdrTarget
from herdrelay.store import Store
from herdrelay.discord_threads import ThreadManager, thread_name


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
        target="w7:p5", workspace_label="herdrelay", agent_name="claude", status="blocked"
    )
    working = HerdrTarget(
        target="w7:p5", workspace_label="herdrelay", agent_name="claude", status="working"
    )
    idle = HerdrTarget(
        target="w7:p5", workspace_label="herdrelay", agent_name="claude", status="idle"
    )

    assert thread_name(blocked).startswith("🔴")
    assert not thread_name(working).startswith("🔴")
    assert not thread_name(idle).startswith("🔴")
    assert thread_name(working) == "herdrelay/claude"


def test_thread_name_identifies_runtime_backend():
    target = HerdrTarget(
        target="local/tab:pane",
        workspace_label="herdr-chat-bridge",
        agent_name="pi",
        status="working",
    )

    assert (
        thread_name(target, backend=BackendKind.ORCA)
        == "[orca] herdr-chat-bridge/pi"
    )
    assert thread_name(target, backend=BackendKind.HERDR_CORE).startswith("[core] ")
    assert thread_name(target, backend=BackendKind.HERDR_TUI).startswith("[tui] ")


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
    client.target_key.side_effect = TargetKey.legacy
    parent = SimpleNamespace(id=10, guild=SimpleNamespace(id=1), create_thread=AsyncMock())
    thread = SimpleNamespace(
        id=123, name="[tui] project/pi", archived=False, edit=AsyncMock()
    )
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

    manager._fetch_thread.return_value.edit.assert_awaited_once_with(
        name="[tui] project/claude"
    )
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
async def test_existing_raw_core_thread_binding_is_pinned_without_duplicate(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_agent_thread(pane_id="t1", thread_id=123, guild_id=1, alias="project")
    store.upsert_binding(
        guild_id=1,
        channel_id=10,
        thread_id=123,
        herdr_target="t1",
        label="project",
        created_by=1,
    )
    client = Mock()
    client.list_panes.return_value = [
        HerdrTarget(target="t1", agent_name="pi", workspace_label="project")
    ]
    client.target_key.return_value = TargetKey.core(
        controller_core_id="core-1", address="t1"
    )
    parent = SimpleNamespace(id=10, guild=SimpleNamespace(id=1), create_thread=AsyncMock())
    thread = SimpleNamespace(id=123, name="project/pi", archived=False, edit=AsyncMock())
    manager = ThreadManager(
        bot=SimpleNamespace(user=None),
        config=AppConfig(discord_token="token"),
        store=store,
        client=client,
    )
    manager._parent_channel = AsyncMock(return_value=parent)
    manager._fetch_thread = AsyncMock(return_value=thread)

    await manager.sync()

    route_target = TargetKey.core(
        controller_core_id="core-1", address="t1"
    ).to_route_value()
    parent.create_thread.assert_not_awaited()
    assert store.get_agent_thread("t1") is None
    assert store.get_agent_thread(route_target) == "123"
    binding = store.find_binding_for_target(route_target)
    assert binding is not None


@pytest.mark.asyncio
async def test_core_thread_binding_pins_core_identity(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    client = Mock()
    client.list_panes.return_value = [
        HerdrTarget(target="t1", agent_name="pi", workspace_label="project")
    ]
    client.target_key.return_value = TargetKey.core(
        controller_core_id="core-1", address="t1"
    )
    parent = SimpleNamespace(
        id=10,
        guild=SimpleNamespace(id=1),
        create_thread=AsyncMock(return_value=SimpleNamespace(id=456)),
    )
    manager = ThreadManager(
        bot=SimpleNamespace(user=None),
        config=AppConfig(discord_token="token"),
        store=store,
        client=client,
    )
    manager._parent_channel = AsyncMock(return_value=parent)
    manager._fetch_thread = AsyncMock()

    await manager.sync()

    route_target = TargetKey.core(
        controller_core_id="core-1", address="t1"
    ).to_route_value()
    assert store.get_agent_thread(route_target) == "456"
    binding = store.find_binding_for_target(route_target)
    assert binding is not None
    assert binding.herdr_target == route_target


@pytest.mark.asyncio
async def test_new_thread_adds_allowed_users_as_members(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    client = Mock()
    client.list_panes.return_value = [
        HerdrTarget(target="w1:p1", agent_name="pi", workspace_label="project")
    ]
    client.target_key.side_effect = TargetKey.legacy
    member = SimpleNamespace(id=42)
    guild = SimpleNamespace(
        id=1,
        get_member=Mock(return_value=member),
        fetch_member=AsyncMock(),
    )
    thread = SimpleNamespace(id=456, add_user=AsyncMock())
    parent = SimpleNamespace(
        id=10,
        guild=guild,
        create_thread=AsyncMock(return_value=thread),
    )
    manager = ThreadManager(
        bot=SimpleNamespace(user=None),
        config=AppConfig(
            discord_token="token", allowed_user_ids=frozenset({42})
        ),
        store=store,
        client=client,
    )
    manager._parent_channel = AsyncMock(return_value=parent)

    await manager.sync()

    thread.add_user.assert_awaited_once_with(member)
    guild.fetch_member.assert_not_awaited()


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
