from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from herdrelay.telegram_api import TelegramApiError, TelegramBotApi


@asynccontextmanager
async def telegram_server(handler):
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_send_text_chunks_plain_text_and_preserves_forum_topic():
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append((request.match_info["method"], payload))
        index = len(requests)
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": index,
                    "message_thread_id": payload.get("message_thread_id"),
                    "chat": {"id": payload["chat_id"], "type": "supergroup"},
                    "text": payload.get("text"),
                },
            }
        )

    async with telegram_server(handler) as base_url:
        async with TelegramBotApi("test-token", base_url=base_url) as api:
            messages = await api.send_text(
                chat_id=-100123,
                message_thread_id=77,
                text="alpha\nbeta\ngamma",
                max_chars=8,
            )

    assert len(messages) == 3
    sent_chunks = [payload["text"] for _method, payload in requests]
    assert "".join(sent_chunks) == "alpha\nbeta\ngamma"
    assert all(method == "sendMessage" for method, _payload in requests)
    assert all(payload["message_thread_id"] == 77 for _method, payload in requests)
    assert all(
        payload["link_preview_options"] == {"is_disabled": True}
        for _method, payload in requests
    )
    assert all("parse_mode" not in payload for _method, payload in requests)


@pytest.mark.asyncio
async def test_send_message_supports_inline_confirmation_and_callback_answer():
    requests = []

    async def handler(request):
        method = request.match_info["method"]
        payload = await request.json()
        requests.append((method, payload))
        if method == "answerCallbackQuery":
            return web.json_response({"ok": True, "result": True})
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "message_thread_id": 77,
                    "chat": {"id": -1001, "type": "supergroup"},
                },
            }
        )

    markup = {
        "inline_keyboard": [
            [{"text": "Interrupt", "callback_data": "interrupt:token"}]
        ]
    }
    async with telegram_server(handler) as base_url:
        async with TelegramBotApi("test-token", base_url=base_url) as api:
            await api.send_message(
                chat_id=-1001,
                message_thread_id=77,
                text="Confirm?",
                reply_markup=markup,
            )
            await api.answer_callback_query(
                callback_query_id="callback-1", text="Interrupting…"
            )

    assert requests[0][1]["reply_markup"] == markup
    assert requests[1] == (
        "answerCallbackQuery",
        {
            "callback_query_id": "callback-1",
            "show_alert": False,
            "text": "Interrupting…",
        },
    )


@pytest.mark.asyncio
async def test_get_updates_uses_outbound_long_polling_parameters():
    requests = []

    async def handler(request):
        payload = await request.json()
        requests.append(payload)
        return web.json_response(
            {"ok": True, "result": [{"update_id": 10, "message": {}}]}
        )

    async with telegram_server(handler) as base_url:
        async with TelegramBotApi("test-token", base_url=base_url) as api:
            updates = await api.get_updates(offset=10, timeout_seconds=25)

    assert updates[0]["update_id"] == 10
    assert requests == [
        {
            "offset": 10,
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        }
    ]


@pytest.mark.asyncio
async def test_forum_topic_methods_use_exact_chat_and_thread_ids():
    requests = []

    async def handler(request):
        method = request.match_info["method"]
        payload = await request.json()
        requests.append((method, payload))
        result = {"message_thread_id": 55, "name": payload.get("name", "topic")}
        if method in {"editForumTopic", "reopenForumTopic", "closeForumTopic"}:
            result = True
        return web.json_response({"ok": True, "result": result})

    async with telegram_server(handler) as base_url:
        async with TelegramBotApi("test-token", base_url=base_url) as api:
            topic = await api.create_forum_topic(chat_id=-1001, name="Core t1")
            await api.edit_forum_topic(
                chat_id=-1001, message_thread_id=55, name="Core t1 idle"
            )
            await api.reopen_forum_topic(chat_id=-1001, message_thread_id=55)
            await api.close_forum_topic(chat_id=-1001, message_thread_id=55)

    assert topic["message_thread_id"] == 55
    assert requests == [
        ("createForumTopic", {"chat_id": -1001, "name": "Core t1"}),
        (
            "editForumTopic",
            {"chat_id": -1001, "message_thread_id": 55, "name": "Core t1 idle"},
        ),
        ("reopenForumTopic", {"chat_id": -1001, "message_thread_id": 55}),
        ("closeForumTopic", {"chat_id": -1001, "message_thread_id": 55}),
    ]


@pytest.mark.asyncio
async def test_api_error_preserves_retry_after_without_retrying():
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return web.json_response(
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
            status=429,
        )

    async with telegram_server(handler) as base_url:
        async with TelegramBotApi("test-token", base_url=base_url) as api:
            with pytest.raises(TelegramApiError, match="Too Many Requests") as caught:
                await api.send_message(chat_id=1, text="hello")

    assert calls == 1
    assert caught.value.error_code == 429
    assert caught.value.retry_after == 7


@pytest.mark.asyncio
async def test_api_timeout_is_not_retried():
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return web.json_response({"ok": True, "result": {}})

    async with telegram_server(handler) as base_url:
        async with TelegramBotApi(
            "test-token", base_url=base_url, request_timeout_seconds=0.02
        ) as api:
            with pytest.raises(TelegramApiError, match="timed out"):
                await api.get_me()

    assert calls == 1


def test_client_rejects_empty_token_and_invalid_chunk_sizes():
    with pytest.raises(ValueError, match="token"):
        TelegramBotApi("")
