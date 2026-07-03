import pytest

from herdr_discord_bridge.config import AppConfig
from herdr_discord_bridge.security import DiscordLocation, SecurityError, SecurityPolicy


def test_read_allowed_when_allowlists_are_empty():
    policy = SecurityPolicy(AppConfig(discord_token="token"))

    policy.ensure_read_allowed(DiscordLocation(guild_id=1, channel_id=2))


def test_send_requires_enable_and_user_allowlist():
    policy = SecurityPolicy(
        AppConfig(
            discord_token="token",
            enable_send=True,
            allowed_user_ids=frozenset({10}),
            max_message_chars=10,
        )
    )
    location = DiscordLocation(guild_id=1, channel_id=2)

    policy.ensure_send_allowed(10, location, "hello")
    with pytest.raises(SecurityError):
        policy.ensure_send_allowed(11, location, "hello")
    with pytest.raises(SecurityError):
        policy.ensure_send_allowed(10, location, "hello world")


def test_send_disabled_even_for_allowed_user():
    policy = SecurityPolicy(
        AppConfig(
            discord_token="token",
            enable_send=False,
            allowed_user_ids=frozenset({10}),
        )
    )

    with pytest.raises(SecurityError, match="Send is disabled"):
        policy.ensure_send_allowed(10, DiscordLocation(guild_id=1, channel_id=2), "hello")


def test_blocklist_is_case_insensitive():
    policy = SecurityPolicy(
        AppConfig(
            discord_token="token",
            enable_send=True,
            allowed_user_ids=frozenset({10}),
            dangerous_text_blocklist=("rm -rf",),
        )
    )

    with pytest.raises(SecurityError):
        policy.ensure_send_allowed(10, DiscordLocation(guild_id=1, channel_id=2), "RM -RF /tmp/x")
