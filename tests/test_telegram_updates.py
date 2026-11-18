from __future__ import annotations

import pytest

from herdr_discord_bridge.config import TelegramConfig
from herdr_discord_bridge.telegram_updates import (
    TelegramUpdatePolicy,
    parse_inbound_message,
)


def update(
    *,
    update_id=10,
    user_id=42,
    chat_id=-1001,
    chat_type="supergroup",
    is_forum=True,
    text="/agents",
    thread_id=77,
    is_bot=False,
):
    message = {
        "message_id": 5,
        "from": {"id": user_id, "is_bot": is_bot},
        "chat": {"id": chat_id, "type": chat_type, "is_forum": is_forum},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": update_id, "message": message}


def policy() -> TelegramUpdatePolicy:
    return TelegramUpdatePolicy(
        TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
        )
    )


def test_parse_authorized_forum_message_and_normalize_command():
    message = parse_inbound_message(update(text="/AGENTS@herdrelay_bot extra"))

    assert message is not None
    assert message.command == "agents"
    assert message.message_thread_id == 77
    assert policy().authorized(message) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": 9},
        {"chat_id": -9},
        {"chat_type": "private", "is_forum": False},
        {"is_forum": False},
    ],
)
def test_policy_rejects_non_allowlisted_or_non_forum_location(changes):
    message = parse_inbound_message(update(**changes))

    assert message is not None
    assert policy().authorized(message) is False


def test_parser_ignores_bot_nontext_and_nonmessage_updates():
    assert parse_inbound_message(update(is_bot=True)) is None
    nontext = update()
    nontext["message"].pop("text")
    assert parse_inbound_message(nontext) is None
    assert parse_inbound_message({"update_id": 10, "callback_query": {}}) is None


def test_parser_rejects_malformed_known_fields():
    with pytest.raises(ValueError, match="update_id"):
        parse_inbound_message({"update_id": True, "message": {}})
    malformed = update()
    malformed["message"]["chat"]["id"] = "-1001"
    with pytest.raises(ValueError, match="chat ID"):
        parse_inbound_message(malformed)
