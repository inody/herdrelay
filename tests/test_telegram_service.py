from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path

import pytest

from herdr_discord_bridge.config import (
    AppConfig,
    HcRuntimeConfig,
    RuntimeConfig,
    TelegramConfig,
)
from herdr_discord_bridge.domain import MutationOutcomeUnknownError
from herdr_discord_bridge.hc_client import HcClient, HcMutationReceipt
from herdr_discord_bridge.hc_events import HcTerminalState
from herdr_discord_bridge.hc_protocol import (
    decode_notification_line,
    parse_subscribe_result,
)
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.telegram_api import TelegramApiError
from herdr_discord_bridge.telegram_service import (
    OFFSET_STATE_KEY,
    TelegramService,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/hc"
SUBSCRIBE = parse_subscribe_result(
    json.loads((FIXTURES / "subscribe.json").read_text())
)
NOTIFICATIONS = [
    decode_notification_line(line)
    for line in (FIXTURES / "notifications.ndjson").read_bytes().splitlines()
]


def update(*, update_id=10, user_id=42, chat_id=-1001, text="/agents"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "message_thread_id": 77,
            "from": {"id": user_id, "is_bot": False},
            "chat": {
                "id": chat_id,
                "type": "supergroup",
                "is_forum": True,
            },
            "text": text,
        },
    }


def callback_update(
    *, update_id=11, user_id=42, data="interrupt:token", callback_id="cb-1"
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id, "is_bot": False},
            "message": {
                "message_id": 6,
                "message_thread_id": 77,
                "chat": {
                    "id": -1001,
                    "type": "supergroup",
                    "is_forum": True,
                },
            },
            "data": data,
        },
    }


def config() -> AppConfig:
    return AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(
            provider="herdr-core",
            core=HcRuntimeConfig(allowed_terminal_addresses=frozenset({"t12"})),
        ),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
        ),
    )


@dataclass
class FakeApi:
    batches: list[list[dict]]
    fail_send: bool = False
    update_calls: list[dict] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    callback_answers: list[dict] = field(default_factory=list)
    closed: bool = False
    created_topics: list[dict] = field(default_factory=list)
    edited_topics: list[dict] = field(default_factory=list)
    reopened_topics: list[dict] = field(default_factory=list)
    closed_topics: list[dict] = field(default_factory=list)

    async def get_updates(self, **kwargs):
        self.update_calls.append(kwargs)
        return self.batches.pop(0) if self.batches else []

    async def send_text(self, **kwargs):
        if self.fail_send:
            raise TelegramApiError("send failed")
        self.sent.append(kwargs)
        return ()

    async def send_message(self, **kwargs):
        if self.fail_send:
            raise TelegramApiError("send failed")
        self.sent.append(kwargs)
        return None

    async def answer_callback_query(self, **kwargs):
        self.callback_answers.append(kwargs)
        return True

    async def create_forum_topic(self, **kwargs):
        self.created_topics.append(kwargs)
        return {"message_thread_id": 100 + len(self.created_topics)}

    async def edit_forum_topic(self, **kwargs):
        self.edited_topics.append(kwargs)
        return True

    async def reopen_forum_topic(self, **kwargs):
        self.reopened_topics.append(kwargs)
        return True

    async def close_forum_topic(self, **kwargs):
        self.closed_topics.append(kwargs)
        return True

    async def close(self):
        self.closed = True


@dataclass
class FakeClient:
    calls: int = 0

    def list_targets(self):
        self.calls += 1
        return [
            HerdrTarget(
                target="t1",
                agent_name="pi",
                status="working",
                workspace_label="project",
            )
        ]


class FakeMutationClient(HcClient):
    def __init__(self, app_config, *, error: Exception | None = None):
        super().__init__(app_config)
        self.error = error
        self.sent = []
        self.interrupted = []

    def list_targets(self):
        self._core_id = "core-1"
        self._core_name = "test-core"
        return [
            HerdrTarget(
                target="t12",
                agent_name="pi",
                status="working",
                workspace_label="herdrelay",
            )
        ]

    def send_with_receipt(self, target, message):
        self.sent.append((target, message))
        if self.error is not None:
            raise self.error
        return HcMutationReceipt(
            observed_turn=4, result={"turn": 5, "started": True}
        )

    def interrupt_with_receipt(self, target):
        self.interrupted.append(target)
        if self.error is not None:
            raise self.error
        return HcMutationReceipt(
            observed_turn=5, result={"outcome": "sent", "key": "esc"}
        )


def service(tmp_path, api: FakeApi, client: FakeClient | None = None):
    client = client or FakeClient()
    result = TelegramService(
        config=config(),
        store=Store(tmp_path / "telegram.sqlite3"),
        client=client,
        api=api,
    )
    return result, client


@pytest.mark.asyncio
async def test_authorized_agents_command_lists_targets_in_same_topic(tmp_path):
    api = FakeApi([[update()]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 1
    assert api.sent == [
        {
            "chat_id": -1001,
            "message_thread_id": 77,
            "text": "Agents\nworking  t1  project/pi",
        }
    ]
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"
    assert api.update_calls[0]["allowed_updates"] == ("message",)


@pytest.mark.asyncio
async def test_unauthorized_update_makes_no_core_or_telegram_send(tmp_path):
    api = FakeApi([[update(user_id=9)]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert api.sent == []
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"


@pytest.mark.asyncio
async def test_normal_text_is_read_only_and_does_not_call_core(tmp_path):
    api = FakeApi([[update(text="please continue")]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert len(api.sent) == 1
    assert "read-only" in api.sent[0]["text"]
    assert "no Core mutation" in api.sent[0]["text"]


@pytest.mark.asyncio
async def test_duplicate_update_is_not_delivered_twice(tmp_path):
    item = update()
    api = FakeApi([[item], [item]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()
    await relay.poll_once()

    assert client.calls == 1
    assert len(api.sent) == 1
    assert api.update_calls[1]["offset"] == 11


@pytest.mark.asyncio
async def test_delivery_failure_does_not_advance_or_dedupe_update(tmp_path):
    api = FakeApi([[update()]], fail_send=True)
    relay, client = service(tmp_path, api)

    with pytest.raises(TelegramApiError, match="send failed"):
        await relay.poll_once()

    assert client.calls == 1
    assert relay.store.get_state(OFFSET_STATE_KEY) is None
    assert relay.store.has_event_key("telegram:update:10") is False


@pytest.mark.asyncio
async def test_invalid_message_is_discarded_and_offset_advances(tmp_path):
    item = update()
    item["message"]["chat"]["id"] = "bad"
    api = FakeApi([[item]])
    relay, client = service(tmp_path, api)

    await relay.poll_once()

    assert client.calls == 0
    assert api.sent == []
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"
    assert relay.store.has_event_key("telegram:update:10") is True


@pytest.mark.asyncio
async def test_status_delivery_reconciles_topic_dedupes_and_closes(tmp_path):
    app_config = config()
    client = HcClient(app_config)
    api = FakeApi([])
    relay = TelegramService(
        config=app_config,
        store=Store(tmp_path / "status.sqlite3"),
        client=client,
        api=api,
    )
    state = HcTerminalState(SUBSCRIBE)

    await relay._reconcile_topics("core-1", state)
    assert api.created_topics == [
        {"chat_id": -1001, "name": "herdrelay/pi · t12"}
    ]
    topic = relay.store.get_telegram_topic(
        core_id="core-1", terminal_address="t12"
    )
    assert topic is not None and topic.thread_id == "101"

    transition = state.apply(NOTIFICATIONS[0])[0]
    await relay._deliver_status("core-1", state, transition)
    await relay._deliver_status("core-1", state, transition)
    assert len(api.sent) == 1
    assert api.sent[0]["message_thread_id"] == 101
    assert api.sent[0]["text"].startswith("🔴 herdrelay/pi\nblocked")

    finished = decode_notification_line(
        '{"jsonrpc":"2.0","method":"event","params":'
        '{"seq":42,"type":"finished","terminal":"t12","data":'
        '{"state":"finished","finished_ms":2000,"exit_code":0,'
        '"signal":null,"reason":"exited"}}}'
    )
    state.apply(finished)
    await relay._handle_lifecycle("core-1", state, finished)
    assert api.closed_topics == [{"chat_id": -1001, "message_thread_id": 101}]
    closed = relay.store.get_telegram_topic(
        core_id="core-1", terminal_address="t12"
    )
    assert closed is not None and closed.closed is True


@pytest.mark.asyncio
async def test_existing_closed_topic_is_reopened_and_renamed(tmp_path):
    app_config = config()
    client = HcClient(app_config)
    api = FakeApi([])
    store = Store(tmp_path / "reopen.sqlite3")
    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="old",
        closed=True,
    )
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )
    target = client.project_terminal(state_terminal := SUBSCRIBE.terminals[0])

    topic = await relay._ensure_topic("core-1", state_terminal.address, target)

    assert api.reopened_topics == [
        {"chat_id": -1001, "message_thread_id": 77}
    ]
    assert api.edited_topics == [
        {
            "chat_id": -1001,
            "message_thread_id": 77,
            "name": "herdrelay/pi · t12",
        }
    ]
    assert topic.closed is False
    assert topic.name == "herdrelay/pi · t12"



def mutation_service(
    tmp_path, api, *, error=None, route_core_id="core-1", enable_interrupt=False
):
    base = config()
    app_config = replace(
        base,
        telegram=replace(
            base.telegram,
            enable_send=True,
            enable_interrupt=enable_interrupt,
        ),
    )
    client = FakeMutationClient(app_config, error=error)
    store = Store(tmp_path / "mutation.sqlite3")
    store.upsert_telegram_topic(
        core_id=route_core_id,
        terminal_address="t12",
        chat_id=-1001,
        thread_id=77,
        name="herdrelay/pi · t12",
    )
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )
    return relay, client


@pytest.mark.asyncio
async def test_enabled_prompt_send_is_claimed_audited_and_not_repeated(tmp_path):
    item = update(text="continue")
    api = FakeApi([[item], [item]])
    relay, client = mutation_service(tmp_path, api)

    await relay.poll_once()
    await relay.poll_once()

    assert client.sent == [("t12", "continue")]
    [audit] = relay.store.list_transport_audits()
    assert audit["transport"] == "telegram"
    assert audit["action"] == "send_text"
    assert audit["outcome"] == "succeeded"
    assert audit["observed_turn"] == 4
    assert audit["payload_preview"] == "continue"
    assert api.sent[-1]["text"] == "✅ Prompt submitted."
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"


@pytest.mark.asyncio
async def test_unknown_prompt_outcome_is_never_retried(tmp_path):
    item = update(text="continue")
    api = FakeApi([[item], [item]])
    relay, client = mutation_service(
        tmp_path,
        api,
        error=MutationOutcomeUnknownError("response lost"),
    )

    await relay.poll_once()
    await relay.poll_once()

    assert client.sent == [("t12", "continue")]
    [audit] = relay.store.list_transport_audits()
    assert audit["outcome"] == "outcome_unknown"
    assert "unknown" in api.sent[-1]["text"]


@pytest.mark.asyncio
async def test_feedback_failure_after_prompt_does_not_retry_mutation(tmp_path):
    api = FakeApi([[update(text="continue")]], fail_send=True)
    relay, client = mutation_service(tmp_path, api)

    await relay.poll_once()

    assert client.sent == [("t12", "continue")]
    [audit] = relay.store.list_transport_audits()
    assert audit["outcome"] == "succeeded"
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"


@pytest.mark.asyncio
async def test_enabled_send_still_rejects_unauthorized_and_blocklisted_text(tmp_path):
    api = FakeApi([[update(user_id=9, text="continue")]])
    relay, client = mutation_service(tmp_path, api)

    await relay.poll_once()

    assert client.sent == []
    assert relay.store.list_transport_audits() == []
    assert api.sent == []

    relay.config = replace(
        relay.config, dangerous_text_blocklist=("sudo",)
    )
    api.batches.append([update(update_id=11, text="sudo reboot")])
    await relay.poll_once()

    assert client.sent == []
    assert relay.store.list_transport_audits() == []
    assert "blocklist" in api.sent[-1]["text"]


@pytest.mark.asyncio
async def test_prompt_requires_topic_route_matching_current_core(tmp_path):
    api = FakeApi([[update(text="continue")]])
    relay, client = mutation_service(
        tmp_path, api, route_core_id="different-core"
    )

    await relay.poll_once()

    assert client.sent == []
    assert relay.store.list_transport_audits() == []
    assert "different Core" in api.sent[-1]["text"]


@pytest.mark.asyncio
async def test_interrupt_requires_one_time_actor_bound_confirmation(tmp_path):
    api = FakeApi([[update(text="/interrupt")]])
    relay, client = mutation_service(tmp_path, api, enable_interrupt=True)

    await relay.poll_once()

    assert client.interrupted == []
    assert api.update_calls[0]["allowed_updates"] == (
        "message",
        "callback_query",
    )
    confirmation_message = api.sent[-1]
    button = confirmation_message["reply_markup"]["inline_keyboard"][0][0]
    callback_data = button["callback_data"]
    assert callback_data.startswith("interrupt:")

    api.batches.append([callback_update(data=callback_data)])
    await relay.poll_once()

    assert client.interrupted == ["t12"]
    [audit] = relay.store.list_transport_audits()
    assert audit["action"] == "interrupt"
    assert audit["outcome"] == "succeeded"
    assert audit["observed_turn"] == 5
    assert api.callback_answers[-1]["text"] == "Interrupting…"
    assert api.sent[-1]["text"] == "✅ Interrupt sent."

    api.batches.append(
        [callback_update(update_id=12, callback_id="cb-2", data=callback_data)]
    )
    await relay.poll_once()

    assert client.interrupted == ["t12"]
    audits = relay.store.list_transport_audits()
    assert [entry["outcome"] for entry in audits] == ["succeeded", "rejected"]
    assert api.callback_answers[-1]["show_alert"] is True


@pytest.mark.asyncio
async def test_interrupt_cancel_consumes_confirmation_without_mutation(tmp_path):
    api = FakeApi([[update(text="/interrupt")]])
    relay, client = mutation_service(tmp_path, api, enable_interrupt=True)
    await relay.poll_once()
    interrupt_data = api.sent[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    token = interrupt_data.split(":", 1)[1]

    api.batches.append([callback_update(data=f"cancel:{token}")])
    await relay.poll_once()

    assert client.interrupted == []
    [audit] = relay.store.list_transport_audits()
    assert audit["outcome"] == "rejected"
    assert audit["detail"] == "cancelled by actor"
    assert api.callback_answers[-1]["text"] == "Cancelled."


@pytest.mark.asyncio
async def test_interrupt_unknown_outcome_is_not_retried(tmp_path):
    api = FakeApi([[update(text="/interrupt")]])
    relay, client = mutation_service(
        tmp_path,
        api,
        enable_interrupt=True,
        error=MutationOutcomeUnknownError("response lost"),
    )
    await relay.poll_once()
    callback_data = api.sent[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]

    api.batches.append([callback_update(data=callback_data)])
    await relay.poll_once()
    api.batches.append(
        [callback_update(update_id=12, callback_id="cb-2", data=callback_data)]
    )
    await relay.poll_once()

    assert client.interrupted == ["t12"]
    audits = relay.store.list_transport_audits()
    assert audits[0]["outcome"] == "outcome_unknown"
    assert audits[1]["outcome"] == "rejected"
    assert "unknown" in api.sent[-1]["text"]
