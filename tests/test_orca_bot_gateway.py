from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import time

import pytest

from herdrelay.config import (
    AppConfig,
    OrcaRuntimeConfig,
    RuntimeConfig,
    TelegramConfig,
    TelegramGeneralRouterConfig,
    TelegramOrcaBotConfig,
)
from herdrelay.orca_bot_gateway import (
    GatewayOutcomeUnknown,
    GatewayQueue,
    OrcaBotGatewayWorker,
    _write_json,
    run_orca_json,
)
from herdrelay.store import Store
from herdrelay.telegram_orca_bot import OrcaBotGatewaySupervisor
from herdrelay.telegram_service import TelegramService


@dataclass
class FakeApi:
    batches: list[list[dict]] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)

    async def get_updates(self, **_kwargs):
        return self.batches.pop(0) if self.batches else []

    async def send_text(self, **kwargs):
        self.sent.append(kwargs)
        return ()

    async def set_message_reaction(self, **kwargs):
        self.reactions.append(kwargs)
        return True

    async def close(self):
        return None


class FakeClient:
    def list_panes(self):
        return []

    def list_targets(self):
        return []


def inbound(text: str, *, update_id: int = 10):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": -1001, "type": "supergroup", "is_forum": True},
            "text": text,
        },
    }


def bot_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        discord_token="unused",
        database_path=str(tmp_path / "store.sqlite3"),
        runtime=RuntimeConfig(
            provider="orca",
            orca=OrcaRuntimeConfig(cli_path="orca", environment="local"),
        ),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            enable_send=True,
            general_router=TelegramGeneralRouterConfig(
                enabled=True,
                provider="orca-bot",
                orca_bot=TelegramOrcaBotConfig(
                    bot_id="bot-1",
                    queue_path=str(tmp_path / "gateway"),
                    gateway_worktree="folder:bot-home",
                    gateway_command="run-gateway",
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_general_orca_bot_enqueues_exact_text_and_claims_update(tmp_path):
    config = bot_config(tmp_path)
    api = FakeApi(batches=[[inbound("Apple Watch request")]])
    store = Store(config.database_path)
    relay = TelegramService(
        config=config,
        store=store,
        client=FakeClient(),
        api=api,
    )

    await relay.poll_once()

    request_path = Path(config.telegram.general_router.orca_bot.queue_path) / (
        "requests/telegram-10.json"
    )
    request = json.loads(request_path.read_text())
    assert request["text"] == "Apple Watch request"
    assert request["kind"] == "send"
    assert api.reactions == [{"chat_id": -1001, "message_id": 5, "emoji": "👀"}]
    [audit] = store.list_transport_audits()
    assert audit["action"] == "bot_send"
    assert audit["outcome"] == "attempt"


@pytest.mark.asyncio
async def test_orca_bot_forget_enqueues_chat_reset(tmp_path):
    config = bot_config(tmp_path)
    api = FakeApi(batches=[[inbound("/forget", update_id=11)]])
    store = Store(config.database_path)
    relay = TelegramService(
        config=config,
        store=store,
        client=FakeClient(),
        api=api,
    )

    await relay.poll_once()

    reset_path = Path(config.telegram.general_router.orca_bot.queue_path) / (
        "requests/reset-11.json"
    )
    assert json.loads(reset_path.read_text())["kind"] == "reset"
    assert api.sent[-1]["text"] == "The General Orca Bot conversation was reset."


@pytest.mark.asyncio
async def test_gateway_reply_is_delivered_to_general_and_audited(tmp_path):
    config = bot_config(tmp_path)
    api = FakeApi()
    store = Store(config.database_path)
    queue = GatewayQueue(config.telegram.general_router.orca_bot.queue_path)
    queue.prepare()
    _write_json(
        queue.events / "telegram-10.reply.json",
        {
            "version": 1,
            "kind": "reply",
            "request_id": "telegram-10",
            "chat_id": -1001,
            "thread_id": None,
            "user_id": 42,
            "text": "Bot answer",
        },
    )
    supervisor = OrcaBotGatewaySupervisor(
        config=config,
        store=store,
        api=api,
        stopped=asyncio.Event(),
        queue=queue,
    )

    await supervisor.drain_events_once()

    assert api.sent == [
        {"chat_id": -1001, "message_thread_id": None, "text": "Bot answer"}
    ]
    [audit] = store.list_transport_audits()
    assert audit["action"] == "bot_reply"
    assert audit["outcome"] == "succeeded"
    assert not (queue.events / "telegram-10.reply.json").exists()


def test_supervisor_replaces_exact_stale_handle_even_if_title_changed(
    tmp_path, monkeypatch
):
    config = bot_config(tmp_path)
    queue = GatewayQueue(config.telegram.general_router.orca_bot.queue_path)
    queue.prepare()
    _write_json(
        queue.heartbeat,
        {
            "version": 1,
            "handle": "term-exact",
            "pane_key": "tab:pane",
            "bot_id": "bot-1",
            "gateway_worktree": "folder:bot-home",
            "updated_at": 0,
        },
    )
    supervisor = OrcaBotGatewaySupervisor(
        config=config,
        store=Store(config.database_path),
        api=FakeApi(),
        stopped=asyncio.Event(),
        queue=queue,
    )
    supervisor._stale_seen_at = (
        time.monotonic()
        - config.telegram.general_router.orca_bot.heartbeat_stale_seconds
        - 1
    )
    monkeypatch.setattr(
        supervisor,
        "_terminal_list",
        lambda: [
            {
                "handle": "term-exact",
                "worktreeId": "folder:bot-home",
                "title": "python",
                "paneTitle": None,
            }
        ],
    )
    closed = []
    created = []
    monkeypatch.setattr(supervisor, "_terminal_close", closed.append)
    monkeypatch.setattr(supervisor, "_terminal_create", lambda: created.append(True))

    supervisor.ensure_worker()

    assert closed == ["term-exact"]
    assert created == [True]


def test_supervisor_does_not_close_same_title_in_another_worktree(
    tmp_path, monkeypatch
):
    config = bot_config(tmp_path)
    queue = GatewayQueue(config.telegram.general_router.orca_bot.queue_path)
    supervisor = OrcaBotGatewaySupervisor(
        config=config,
        store=Store(config.database_path),
        api=FakeApi(),
        stopped=asyncio.Event(),
        queue=queue,
    )
    monkeypatch.setattr(
        supervisor,
        "_terminal_list",
        lambda: [
            {
                "handle": "term-user",
                "worktreeId": "folder:someone-else",
                "title": "HerdRelay Gateway",
                "paneTitle": "HerdRelay Gateway",
            }
        ],
    )
    closed = []
    created = []
    monkeypatch.setattr(supervisor, "_terminal_close", closed.append)
    monkeypatch.setattr(supervisor, "_terminal_create", lambda: created.append(True))

    supervisor.ensure_worker()

    assert closed == []
    assert created == [True]


def test_shared_orca_runner_classifies_mutation_timeout_as_unknown(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["orca"], 5)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(GatewayOutcomeUnknown, match="timed out"):
        run_orca_json(
            cli_path="orca",
            environment="local",
            timeout_seconds=5,
            args=["orchestration", "send"],
            mutation=True,
        )


def test_gateway_worker_recovers_inflight_send_as_outcome_unknown(
    tmp_path, monkeypatch
):
    config = bot_config(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-gateway")
    worker = OrcaBotGatewayWorker(config)
    worker.queue.prepare()
    _write_json(
        worker.queue.processing / "telegram-10.json",
        {
            "version": 1,
            "kind": "send",
            "request_id": "telegram-10",
            "audit_id": 7,
            "chat_id": -1001,
            "thread_id": None,
            "user_id": 42,
            "message_id": 5,
            "source_kind": "text",
            "text": "maybe sent",
        },
    )

    worker._recover_processing()

    [event_path] = worker.queue.event_paths()
    event = json.loads(event_path.read_text())
    assert event["kind"] == "outcome_unknown"
    assert not (worker.queue.processing / "telegram-10.json").exists()


def test_gateway_worker_correlates_reply_with_outbound_message(tmp_path, monkeypatch):
    config = bot_config(tmp_path)
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term-gateway")
    monkeypatch.setenv("ORCA_PANE_KEY", "tab:pane")
    worker = OrcaBotGatewayWorker(config)
    worker.queue.enqueue(
        {
            "version": 1,
            "kind": "send",
            "request_id": "telegram-10",
            "audit_id": 7,
            "event_key": "telegram:update:10",
            "chat_id": -1001,
            "thread_id": None,
            "user_id": 42,
            "message_id": 5,
            "source_kind": "text",
            "text": "hello bot",
        }
    )
    consumed = []

    def fake_orca(args, *, mutation=False):
        if args[:2] == ["orchestration", "send"]:
            assert "--chat" in args and args[args.index("--chat") + 1] == "new"
            return {
                "result": {
                    "message": {
                        "id": "msg-outbound",
                        "to_handle": "orca_session_id:chat-1",
                    }
                }
            }
        if "--peek" in args:
            return {
                "result": {
                    "messages": [
                        {
                            "id": "msg-reply",
                            "thread_id": "msg-outbound",
                            "body": "hello owner",
                        }
                    ]
                }
            }
        consumed.append(args)
        return {"result": {"messages": []}}

    monkeypatch.setattr(worker, "_orca_json", fake_orca)

    worker._process_requests()
    worker._process_replies()

    events = {path.name: json.loads(path.read_text()) for path in worker.queue.event_paths()}
    assert events["telegram-10.sent.json"]["bot_chat_id"] == "chat-1"
    assert events["telegram-10.reply.json"]["text"] == "hello owner"
    assert json.loads(worker.queue.chat_state.read_text())["chat_id"] == "chat-1"
    assert list(worker.queue.pending.glob("*.json")) == []
    assert consumed
