from __future__ import annotations

import pytest

from herdrelay.config import TelegramConfig
from herdrelay.telegram_updates import (
    TelegramUpdatePolicy,
    parse_inbound_callback,
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


def test_double_slash_escapes_agent_command():
    message = parse_inbound_message(update(text="//reload"))

    assert message is not None
    assert message.command is None
    assert message.prompt_text == "/reload"


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


def test_parser_accepts_voice_message_without_text():
    raw = update(text="", thread_id=None)
    raw["message"].pop("text")
    raw["message"]["voice"] = {
        "file_id": "voice-file",
        "file_unique_id": "voice-unique",
        "duration": 12,
        "mime_type": "audio/ogg",
        "file_size": 1234,
    }

    message = parse_inbound_message(raw)

    assert message is not None
    assert message.text == ""
    assert message.voice is not None
    assert message.voice.file_id == "voice-file"
    assert message.voice.duration_seconds == 12
    assert policy().authorized(message) is True


def test_parser_ignores_bot_nontext_and_nonmessage_updates():
    assert parse_inbound_message(update(is_bot=True)) is None
    nontext = update()
    nontext["message"].pop("text")
    assert parse_inbound_message(nontext) is None
    assert parse_inbound_message({"update_id": 10, "callback_query": {}}) is None


def test_parse_authorized_callback_query():
    raw = {
        "update_id": 11,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 42, "is_bot": False},
            "message": {
                "message_id": 6,
                "message_thread_id": 77,
                "chat": {
                    "id": -1001,
                    "type": "supergroup",
                    "is_forum": True,
                },
            },
            "data": "interrupt:token",
        },
    }

    callback = parse_inbound_callback(raw)

    assert callback is not None
    assert callback.callback_query_id == "callback-1"
    assert callback.data == "interrupt:token"
    assert policy().authorized(callback) is True


def test_parser_rejects_malformed_known_fields():
    with pytest.raises(ValueError, match="update_id"):
        parse_inbound_message({"update_id": True, "message": {}})
    malformed = update()
    malformed["message"]["chat"]["id"] = "-1001"
    with pytest.raises(ValueError, match="chat ID"):
        parse_inbound_message(malformed)
