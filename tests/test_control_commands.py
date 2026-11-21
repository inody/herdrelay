from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from herdrelay.config import AppConfig, HcRuntimeConfig, RuntimeConfig
from herdrelay.discord_bot import HerdrDiscordBot
from herdrelay.discord_security import SecurityPolicy
from herdrelay.domain import TargetKey
from herdrelay.hc_client import HcClient
from herdrelay.herdr_client import HerdrClient
from herdrelay.models import HerdrTarget
from herdrelay.store import Store


def make_config(*, core=False, enable_interrupt=False):
    return AppConfig(
        discord_token="not-used",
        enable_send=True,
        enable_interrupt=enable_interrupt,
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({2}),
        allowed_user_ids=frozenset({10}),
        dangerous_text_blocklist=("sudo", "rm -rf"),
        runtime=(
            RuntimeConfig(provider="herdr-core", core=HcRuntimeConfig())
            if core
            else RuntimeConfig()
        ),
    )


def make_bot(tmp_path, *, core=False, enable_interrupt=False):
    config = make_config(core=core, enable_interrupt=enable_interrupt)
    client = Mock(spec=HcClient if core else HerdrClient)
    client.capabilities = (HcClient if core else HerdrClient).capabilities
    client.list_targets.return_value = [
        HerdrTarget(target="t1" if core else "w1:p1", agent_name="pi", status="idle")
    ]
    if core:
        client.resolve_target_key.return_value = HerdrTarget(
            target="t1", agent_name="pi", status="working"
        )
    else:
        client.resolve_target_key.return_value = HerdrTarget(
            target="w1:p1", agent_name="pi", status="idle"
        )
    bot = HerdrDiscordBot(
        config=config,
        store=Store(tmp_path / ("core.sqlite3" if core else "legacy.sqlite3")),
        client=client,
    )
    bot._connection.user = SimpleNamespace(id=99)
    return bot


def configure(bot, **changes):
    bot.config = replace(bot.config, **changes)
    bot.security = SecurityPolicy(bot.config)


def message(bot, text, *, user=10, guild=1, channel=2, thread=None, nickname=False):
    return SimpleNamespace(
        content=f"<@{'!' if nickname else ''}99> {text}",
        mentions=[bot.user],
        author=SimpleNamespace(id=user, bot=False),
        guild=SimpleNamespace(id=guild) if guild is not None else None,
        channel=SimpleNamespace(id=thread or channel, parent_id=channel if thread else None),
        reply=AsyncMock(),
    )


def audits(bot):
    with bot.store._connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id")]


def bind_core_thread(bot):
    bot.store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=3,
        herdr_target=TargetKey.core(
            controller_core_id="core-1", address="t1"
        ).to_route_value(),
        label="test",
        created_by=10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["list", "help", "interrupt t1", "bogus"])
@pytest.mark.parametrize(
    "location",
    [{"guild": 9}, {"channel": 9}, {"channel": 9, "thread": 3}, {"guild": None}],
)
async def test_all_commands_check_location_before_backend_access(tmp_path, command, location):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    msg = message(bot, command, **location)

    await bot.on_message(msg)

    assert bot.client.mock_calls == []
    assert audits(bot)[0]["result"].startswith("denied:")


@pytest.mark.asyncio
async def test_literal_herdrelay_prefix_is_a_control_command(tmp_path):
    bot = make_bot(tmp_path)
    msg = message(bot, "help")
    msg.content = "@HerdRelay help"
    msg.mentions = []

    await bot.on_message(msg)

    assert audits(bot)[0]["action"] == "control_help"
    msg.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["list", "help", "?", ""])
async def test_read_commands_do_not_require_write_user(tmp_path, command):
    bot = make_bot(tmp_path)
    configure(bot, enable_send=False, allowed_user_ids=frozenset())
    msg = message(bot, command, user=11)

    await bot.on_message(msg)

    assert audits(bot)[0]["result"] == "ok"
    msg.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["user", "empty_users", "disabled"])
async def test_interrupt_rejected_before_backend_access(tmp_path, reason):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    user = 11 if reason == "user" else 10
    if reason == "empty_users":
        configure(bot, allowed_user_ids=frozenset())
    if reason == "disabled":
        configure(bot, enable_interrupt=False)

    await bot.on_message(message(bot, "interrupt t1", user=user))

    bot.client.interrupt.assert_not_called()
    assert audits(bot)[0]["result"].startswith("denied:")


@pytest.mark.asyncio
async def test_interrupt_is_audited_before_and_after_mutation(tmp_path):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)

    def interrupt(target):
        assert target == "t1"
        assert [entry["result"] for entry in audits(bot)] == ["attempt"]

    bot.client.interrupt.side_effect = interrupt
    msg = message(bot, "interrupt t1")

    await bot.on_message(msg)

    bot.client.interrupt.assert_called_once_with("t1")
    assert [entry["result"] for entry in audits(bot)] == ["attempt", "ok"]
    assert msg.reply.call_args.args[0] == "Interrupted `t1`."


@pytest.mark.asyncio
async def test_interrupt_resolves_bound_thread_target(tmp_path):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    bind_core_thread(bot)

    await bot.on_message(message(bot, "interrupt", thread=3))

    bot.client.interrupt.assert_called_once_with("t1")
    assert {entry["herdr_target"] for entry in audits(bot)} == {"t1"}


@pytest.mark.asyncio
async def test_stop_is_compatibility_alias_for_interrupt_only(tmp_path):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)

    await bot.on_message(message(bot, "stop t1"))

    bot.client.interrupt.assert_called_once_with("t1")
    assert {entry["action"] for entry in audits(bot)} == {"control_interrupt"}


@pytest.mark.asyncio
async def test_legacy_backend_cannot_close_target_via_stop_alias(tmp_path):
    bot = make_bot(tmp_path)
    configure(bot, enable_interrupt=True)

    await bot.on_message(message(bot, "stop w1:p1"))

    assert audits(bot)[-1]["result"].startswith("error: Interrupt is unavailable")


@pytest.mark.asyncio
async def test_help_lists_only_supported_enabled_controls(tmp_path):
    core_bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    legacy_bot = make_bot(tmp_path)

    core_message = message(core_bot, "help")
    legacy_message = message(legacy_bot, "help")
    await core_bot.on_message(core_message)
    await legacy_bot.on_message(legacy_message)

    assert "@HerdRelay interrupt" in core_message.reply.call_args.args[0]
    assert "@HerdRelay interrupt" not in legacy_message.reply.call_args.args[0]
    assert "start" not in legacy_message.reply.call_args.args[0]
    assert "close" not in legacy_message.reply.call_args.args[0]


@pytest.mark.asyncio
async def test_removed_start_command_is_rejected_without_mutation(tmp_path):
    bot = make_bot(tmp_path)

    await bot.on_message(message(bot, "start pi"))

    assert audits(bot)[0]["action"] == "control_unknown"
    assert audits(bot)[0]["result"].startswith("error:")


@pytest.mark.asyncio
async def test_backend_interrupt_failure_is_audited(tmp_path):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    bot.client.interrupt.side_effect = RuntimeError("backend failed")

    await bot.on_message(message(bot, "interrupt t1"))

    assert [entry["result"] for entry in audits(bot)] == [
        "attempt",
        "error: backend failed",
    ]


@pytest.mark.asyncio
async def test_audit_failure_prevents_interrupt(tmp_path, monkeypatch, caplog):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    monkeypatch.setattr(
        bot.store, "add_audit", Mock(side_effect=RuntimeError("audit unavailable"))
    )

    await bot.on_message(message(bot, "interrupt t1"))

    bot.client.interrupt.assert_not_called()
    assert "Failed to audit" in caplog.text


@pytest.mark.asyncio
async def test_error_echo_disables_mentions(tmp_path):
    bot = make_bot(tmp_path)
    msg = message(bot, "@everyone")

    await bot.on_message(msg)

    kwargs = msg.reply.call_args.kwargs
    assert not kwargs["allowed_mentions"].everyone
    assert not kwargs["allowed_mentions"].users
    assert not kwargs["allowed_mentions"].roles
    assert kwargs["mention_author"] is False


@pytest.mark.asyncio
async def test_bot_messages_cannot_issue_commands(tmp_path):
    bot = make_bot(tmp_path, core=True, enable_interrupt=True)
    msg = message(bot, "interrupt t1")
    msg.author.bot = True

    await bot.on_message(msg)

    bot.client.interrupt.assert_not_called()
    assert audits(bot) == []
