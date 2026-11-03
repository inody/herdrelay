from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from herdr_discord_bridge.config import AppConfig
from herdr_discord_bridge.discord_bot import HerdrDiscordBot
from herdr_discord_bridge.herdr_client import HerdrClient, StartedAgent
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.security import SecurityPolicy
from herdr_discord_bridge.store import Store


@pytest.fixture
def bot(tmp_path, monkeypatch):
    config = AppConfig(
        discord_token="not-used", enable_send=True, enable_stop=True,
        allowed_guild_ids=frozenset({1}), allowed_channel_ids=frozenset({2}),
        allowed_user_ids=frozenset({10}), dangerous_text_blocklist=("sudo", "rm -rf"),
    )
    client = Mock(spec=HerdrClient)
    client.agent_start.return_value = StartedAgent(pane_id="w1:p9", result={})
    client.list_targets.return_value = [HerdrTarget(
        target="w1:p1", agent_name="pi", status="idle", cwd="/tmp/project",
        raw={"workspace_id": "w1", "tab_id": "w1:t1"},
    )]
    client.read.return_value = "ready"
    result = HerdrDiscordBot(config=config, store=Store(tmp_path / "test.sqlite3"), client=client)
    result._connection.user = SimpleNamespace(id=99)
    monkeypatch.setattr("herdr_discord_bridge.discord_bot.asyncio.sleep", AsyncMock())
    return result


def configure(bot, **changes):
    bot.config = replace(bot.config, **changes)
    bot.security = SecurityPolicy(bot.config)


def message(bot, text, *, user=10, guild=1, channel=2, thread=None, nickname=False):
    return SimpleNamespace(
        content=f"<@{'!' if nickname else ''}99> {text}",
        mentions=[bot.user], author=SimpleNamespace(id=user, bot=False),
        guild=SimpleNamespace(id=guild) if guild is not None else None,
        channel=SimpleNamespace(id=thread or channel, parent_id=channel if thread else None),
        reply=AsyncMock(),
    )


def audits(bot):
    with bot.store._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id")]


def bind_thread(bot):
    bot.store.upsert_binding(
        guild_id=1, channel_id=2, thread_id=3, herdr_target="w1:p1",
        label="test", created_by=10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["start pi", "stop w1:p1", "stop"])
@pytest.mark.parametrize("reason", ["user", "empty_users", "disabled"])
async def test_mutations_rejected_before_any_herdr_access(bot, command, reason):
    bind_thread(bot)
    user = 11 if reason == "user" else 10
    if reason == "empty_users":
        configure(bot, allowed_user_ids=frozenset())
    if reason == "disabled":
        configure(bot, **{"enable_send" if command.startswith("start") else "enable_stop": False})
    msg = message(bot, command, user=user, thread=3)

    await bot.on_message(msg)

    assert bot.client.mock_calls == []
    msg.reply.assert_awaited_once()
    assert msg.reply.call_args.args[0].startswith("Error:")
    [entry] = audits(bot)
    assert entry["result"].startswith("denied:")
    assert entry["action"] == f"control_{command.split()[0]}"
    assert (entry["discord_user_id"], entry["guild_id"], entry["channel_id"], entry["thread_id"]) == (
        str(user), "1", "2", "3",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["start pi", "stop w1:p1", "list", "help", "?", "", "bogus"])
@pytest.mark.parametrize("location", [
    {"guild": 9}, {"channel": 9}, {"channel": 9, "thread": 3}, {"guild": None},
])
async def test_all_mention_commands_check_location_before_dispatch(bot, command, location):
    msg = message(bot, command, **location)

    await bot.on_message(msg)

    assert bot.client.mock_calls == []
    assert audits(bot)[0]["result"].startswith("denied:")
    assert "Herdr control" not in msg.reply.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["start pi -- SUDO something", "start pi -- rm -rf /tmp/test", "start pi " + "x" * 2000])
async def test_start_uses_message_length_and_blocklist_guards(bot, text):
    await bot.on_message(message(bot, text))

    assert bot.client.mock_calls == []
    [entry] = audits(bot)
    assert entry["result"].startswith("denied:")
    assert len(entry["payload_preview"]) <= 200


@pytest.mark.asyncio
async def test_start_authorized_and_audited_before_mutation_and_reply(bot):
    msg = message(bot, "START pi -- --model test", nickname=True)
    def start(*args, **kwargs):
        assert [entry["result"] for entry in audits(bot)] == ["attempt"]
        return StartedAgent(pane_id="w1:p9", result={})
    bot.client.agent_start.side_effect = start
    async def reply(*args, **kwargs):
        assert audits(bot)[-1]["result"] == "ok"
    msg.reply.side_effect = reply

    await bot.on_message(msg)

    bot.client.agent_start.assert_called_once_with(
        "pi", cwd=None, argv=["--model", "test"], workspace=None, tab=None, split=None, source_pane=None,
    )
    entries = audits(bot)
    assert [entry["result"] for entry in entries] == ["attempt", "ok"]
    assert entries[-1]["herdr_target"] == "w1:p9"
    assert entries[-1]["action"] == "control_start"


@pytest.mark.asyncio
async def test_authorized_start_inherits_bound_pane_context(bot):
    bind_thread(bot)

    await bot.on_message(message(bot, "start pi", thread=3))

    bot.client.agent_start.assert_called_once_with(
        "pi", cwd="/tmp/project", argv=None, workspace="w1", tab="w1:t1", split=None, source_pane="w1:p1",
    )
    assert audits(bot)[-1]["thread_id"] == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_stop_audits_actual_explicit_or_bound_target_before_closing(bot, explicit):
    bind_thread(bot)
    target = "w2:p4" if explicit else "w1:p1"
    msg = message(bot, f"stop {target}" if explicit else "stop", thread=3)
    def close(pane_id):
        assert pane_id == target
        [entry] = audits(bot)
        assert entry["result"] == "attempt"
        assert entry["herdr_target"] == target
    bot.client.pane_close.side_effect = close

    await bot.on_message(msg)

    bot.client.pane_close.assert_called_once_with(target)
    entries = audits(bot)
    assert [entry["result"] for entry in entries] == ["attempt", "ok"]
    assert {entry["herdr_target"] for entry in entries} == {target}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["list", "help", "?", ""])
async def test_read_commands_need_location_but_not_write_flag_or_write_user(bot, command):
    configure(bot, enable_send=False, enable_stop=False, allowed_user_ids=frozenset())
    msg = message(bot, command, user=11)

    await bot.on_message(msg)

    [entry] = audits(bot)
    assert entry["result"] == "ok"
    assert entry["action"] == ("control_list" if command == "list" else "control_help")
    bot.client.agent_start.assert_not_called()
    bot.client.pane_close.assert_not_called()
    msg.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("command, changes, method", [
    ("start pi", {"enable_stop": False}, "agent_start"),
    ("stop w1:p1", {"enable_send": False}, "pane_close"),
])
async def test_mutations_use_their_own_feature_flag(bot, command, changes, method):
    configure(bot, **changes)

    await bot.on_message(message(bot, command))

    getattr(bot.client, method).assert_called_once()
    assert audits(bot)[-1]["result"] == "ok"


@pytest.mark.asyncio
async def test_thread_id_allowlist_also_allows_mention_commands(bot):
    configure(bot, allowed_channel_ids=frozenset({3}))

    await bot.on_message(message(bot, "stop w1:p1", thread=3))

    bot.client.pane_close.assert_called_once_with("w1:p1")
    assert audits(bot)[-1]["result"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["start", "stop", "bogus"])
async def test_invalid_commands_are_audited_without_mutations(bot, command):
    await bot.on_message(message(bot, command))

    assert bot.client.mock_calls == []
    [entry] = audits(bot)
    assert entry["result"].startswith("error:")
    assert entry["action"] == f"control_{command if command != 'bogus' else 'unknown'}"


@pytest.mark.asyncio
@pytest.mark.parametrize("command, method", [("start pi", "agent_start"), ("stop w1:p1", "pane_close"), ("list", "list_targets")])
async def test_backend_failures_are_audited(bot, command, method):
    getattr(bot.client, method).side_effect = RuntimeError("backend failed")

    await bot.on_message(message(bot, command))

    entries = audits(bot)
    assert entries[-1]["result"] == "error: backend failed"
    assert not any(entry["result"] == "ok" for entry in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["start pi", "stop w1:p1"])
async def test_audit_failure_prevents_mutation(bot, command, monkeypatch, caplog):
    monkeypatch.setattr(bot.store, "add_audit", Mock(side_effect=RuntimeError("audit unavailable")))

    await bot.on_message(message(bot, command))

    assert bot.client.mock_calls == []
    assert "Failed to audit" in caplog.text


@pytest.mark.asyncio
async def test_successful_close_stays_recorded_if_discord_reply_fails(bot):
    msg = message(bot, "stop w1:p1")
    msg.reply.side_effect = [RuntimeError("reply failed"), None]

    await bot.on_message(msg)

    bot.client.pane_close.assert_called_once_with("w1:p1")
    assert [entry["result"] for entry in audits(bot)] == ["attempt", "ok", "error: reply failed"]


@pytest.mark.asyncio
async def test_error_echo_does_not_trigger_mentions(bot):
    msg = message(bot, "@everyone")

    await bot.on_message(msg)

    kwargs = msg.reply.call_args.kwargs
    assert not kwargs["allowed_mentions"].everyone
    assert not kwargs["allowed_mentions"].users
    assert not kwargs["allowed_mentions"].roles
    assert kwargs["mention_author"] is False


@pytest.mark.asyncio
async def test_bot_messages_cannot_issue_control_commands(bot):
    msg = message(bot, "stop w1:p1")
    msg.author.bot = True

    await bot.on_message(msg)

    assert bot.client.mock_calls == []
    assert audits(bot) == []
    msg.reply.assert_not_awaited()
