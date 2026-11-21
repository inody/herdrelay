from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdrelay.config import (
    AppConfig,
    HcRuntimeConfig,
    HcTargetExposureConfig,
    RuntimeConfig,
    TelegramConfig,
)
from herdrelay.domain import (
    MutationOutcomeUnknownError,
    MutationReceipt,
    TargetKey,
)
from herdrelay.hc_client import HcClient, HcMutationReceipt
from herdrelay.hc_events import HcTerminalState
from herdrelay.hc_protocol import (
    decode_notification_line,
    parse_subscribe_result,
)
from herdrelay.models import HerdrTarget
from herdrelay.store import Store
from herdrelay.telegram_api import TelegramApiError
from herdrelay.telegram_service import (
    OFFSET_STATE_KEY,
    TelegramService,
)
from herdrelay.telegram_topics import TelegramTopicManager

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
            core=HcRuntimeConfig(
                target_exposure=HcTargetExposureConfig(
                    mode="selected", addresses=frozenset({"t12"})
                )
            ),
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
    reopen_error: Exception | None = None
    close_error: Exception | None = None
    delete_errors: dict[int, Exception] = field(default_factory=dict)
    update_calls: list[dict] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    callback_answers: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    closed: bool = False
    created_topics: list[dict] = field(default_factory=list)
    edited_topics: list[dict] = field(default_factory=list)
    reopened_topics: list[dict] = field(default_factory=list)
    closed_topics: list[dict] = field(default_factory=list)
    deleted_topics: list[dict] = field(default_factory=list)

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

    async def set_message_reaction(self, **kwargs):
        if self.fail_send:
            raise TelegramApiError("reaction failed")
        self.reactions.append(kwargs)
        return True

    async def create_forum_topic(self, **kwargs):
        self.created_topics.append(kwargs)
        return {"message_thread_id": 100 + len(self.created_topics)}

    async def edit_forum_topic(self, **kwargs):
        self.edited_topics.append(kwargs)
        return True

    async def reopen_forum_topic(self, **kwargs):
        self.reopened_topics.append(kwargs)
        if self.reopen_error is not None:
            raise self.reopen_error
        return True

    async def close_forum_topic(self, **kwargs):
        self.closed_topics.append(kwargs)
        if self.close_error is not None:
            raise self.close_error
        return True

    async def delete_forum_topic(self, **kwargs):
        self.deleted_topics.append(kwargs)
        error = self.delete_errors.get(kwargs["message_thread_id"])
        if error is not None:
            raise error
        return True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_topic_manager_reuses_topic_after_orca_runtime_rotation(tmp_path):
    app_config = replace(
        config(),
        telegram=replace(config().telegram, forum_chat_id=-1001),
    )
    store = Store(tmp_path / "store.sqlite3")
    api = FakeApi(batches=[])
    manager = TelegramTopicManager(config=app_config, store=store, api=api)
    old = TargetKey.orca(instance_id="runtime-old", address="local/tab:pane")
    new = TargetKey.orca(instance_id="runtime-new", address="local/tab:pane")
    target = HerdrTarget(
        target=new.address,
        agent_name="claude",
        status="working",
        workspace_label="project",
    )
    store.upsert_telegram_topic_for_target(
        target=old,
        chat_id=-1001,
        thread_id=77,
        name="project/claude · local/tab:pane",
    )

    route = await manager.ensure(new, target)

    assert api.created_topics == []
    assert route.thread_id == "77"
    assert route.route_target == new.to_route_value()
    assert len(store.list_telegram_topics()) == 1


@pytest.mark.asyncio
async def test_topic_creation_conflict_deletes_new_topic_and_reuses_existing(
    tmp_path, monkeypatch
):
    app_config = replace(
        config(),
        telegram=replace(config().telegram, forum_chat_id=-1001),
    )
    store = Store(tmp_path / "store.sqlite3")
    api = FakeApi(batches=[])
    manager = TelegramTopicManager(config=app_config, store=store, api=api)
    old = TargetKey.orca(instance_id="runtime-old", address="local/tab:pane")
    new = TargetKey.orca(instance_id="runtime-new", address="local/tab:pane")
    target = HerdrTarget(
        target=new.address,
        agent_name="claude",
        status="working",
        workspace_label="project",
    )
    store.upsert_telegram_topic_for_target(
        target=old,
        chat_id=-1001,
        thread_id=77,
        name="project/claude · local/tab:pane",
    )
    original_get = store.get_telegram_topic_for_target
    calls = 0

    def delayed_existing(key):
        nonlocal calls
        calls += 1
        return None if calls <= 2 else original_get(key)

    monkeypatch.setattr(store, "get_telegram_topic_for_target", delayed_existing)
    original_upsert = store.upsert_telegram_topic_for_target
    upsert_calls = 0

    def fail_once(**kwargs):
        nonlocal upsert_calls
        upsert_calls += 1
        if upsert_calls == 1:
            raise sqlite3.IntegrityError("stable route conflict")
        return original_upsert(**kwargs)

    monkeypatch.setattr(store, "upsert_telegram_topic_for_target", fail_once)

    route = await manager.ensure(new, target)

    assert route.thread_id == "77"
    assert api.created_topics == [
        {"chat_id": -1001, "name": "[orca] project/claude · local/tab:pane"}
    ]
    assert api.deleted_topics == [
        {"chat_id": -1001, "message_thread_id": 101}
    ]


@pytest.mark.asyncio
async def test_topic_persistence_failure_deletes_new_topic_and_enters_cooldown(
    tmp_path, monkeypatch
):
    app_config = replace(
        config(),
        telegram=replace(config().telegram, forum_chat_id=-1001),
    )
    store = Store(tmp_path / "store.sqlite3")
    api = FakeApi(batches=[])
    manager = TelegramTopicManager(config=app_config, store=store, api=api)
    key = TargetKey.orca(instance_id="runtime", address="local/tab:pane")
    target = HerdrTarget(
        target=key.address,
        agent_name="claude",
        status="working",
        workspace_label="project",
    )
    monkeypatch.setattr(
        store,
        "upsert_telegram_topic_for_target",
        lambda **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database unavailable")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        await manager.ensure(key, target)
    with pytest.raises(RuntimeError, match="cooling down"):
        await manager.ensure(key, target)

    assert len(api.created_topics) == 1
    assert api.deleted_topics == [
        {"chat_id": -1001, "message_thread_id": 101}
    ]


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


@dataclass
class FakeLegacyClient:
    panes: list[HerdrTarget]
    sent: list[tuple[str, str]] = field(default_factory=list)

    def list_panes(self):
        return list(self.panes)

    def list_targets(self):
        return [target for target in self.panes if target.agent_name]

    def target_key(self, target):
        return TargetKey.legacy(target)

    def resolve_target_key(self, key):
        return next(target for target in self.panes if target.target == key.address)

    def send(self, target, text):
        self.sent.append((target, text))
        return MutationReceipt(observed_turn=None, result={})


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

    def resolve_target_key(self, key):
        [target] = self.list_targets()
        if key != self.target_key(target.target):
            raise ValueError("target identity mismatch")
        return target

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
    assert "no runtime mutation" in api.sent[0]["text"]


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

    await relay.status_relay.reconcile_core_topics("core-1", state)
    assert api.created_topics == [
        {"chat_id": -1001, "name": "[hc] herdrelay/pi · t12"}
    ]
    topic = relay.store.get_telegram_topic(
        core_id="core-1", terminal_address="t12"
    )
    assert topic is not None and topic.thread_id == "101"

    transition = state.apply(NOTIFICATIONS[0])[0]
    await relay.status_relay.deliver_core_status("core-1", state, transition)
    await relay.status_relay.deliver_core_status("core-1", state, transition)
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
    await relay.status_relay.handle_core_lifecycle("core-1", state, finished)
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

    topic = await relay.status_relay.ensure_core_topic(
        "core-1", state_terminal.address, target
    )

    assert api.reopened_topics == [
        {"chat_id": -1001, "message_thread_id": 77}
    ]
    assert api.edited_topics == [
        {
            "chat_id": -1001,
            "message_thread_id": 77,
            "name": "[hc] herdrelay/pi · t12",
        }
    ]
    assert topic.closed is False
    assert topic.name == "[hc] herdrelay/pi · t12"


@pytest.mark.asyncio
async def test_topic_state_updates_when_telegram_reports_already_in_desired_state(
    tmp_path,
):
    app_config = config()
    client = HcClient(app_config)
    store = Store(tmp_path / "idempotent.sqlite3")
    key = TargetKey.core(controller_core_id="core-1", address="t12")
    store.upsert_telegram_topic_for_target(
        target=key,
        chat_id=-1001,
        thread_id=77,
        name="herdrelay/pi · t12",
        closed=True,
    )
    api = FakeApi(
        [],
        reopen_error=TelegramApiError("Bad Request: TOPIC_NOT_MODIFIED"),
        close_error=TelegramApiError("Bad Request: TOPIC_NOT_MODIFIED"),
    )
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )
    target = client.project_terminal(SUBSCRIBE.terminals[0])

    reopened = await relay.status_relay.ensure_core_topic("core-1", "t12", target)
    assert reopened.closed is False

    await relay.topics.close(key)
    closed = store.get_telegram_topic_for_target(key)
    assert closed is not None and closed.closed is True


@pytest.mark.asyncio
async def test_closed_topic_is_deleted_after_minute_retention_period(tmp_path):
    base = config()
    app_config = replace(
        base,
        telegram=replace(base.telegram, closed_topic_retention_minutes=10),
    )
    api = FakeApi([])
    store = Store(tmp_path / "retention.sqlite3")
    key = TargetKey.core(controller_core_id="core-1", address="t12")
    store.upsert_telegram_topic_for_target(
        target=key,
        chat_id=-1001,
        thread_id=77,
        name="herdrelay/pi · t12",
        closed=True,
    )
    relay = TelegramService(
        config=app_config,
        store=store,
        client=HcClient(app_config),
        api=api,
    )

    deleted = await relay.topics.cleanup_expired_closed(
        now=datetime.now(UTC) + timedelta(minutes=20)
    )

    assert deleted == (77,)
    assert api.deleted_topics == [
        {"chat_id": -1001, "message_thread_id": 77}
    ]
    assert store.get_telegram_topic_for_target(key) is None


@pytest.mark.asyncio
async def test_closed_topic_is_kept_within_minute_retention_period(tmp_path):
    base = config()
    app_config = replace(
        base,
        telegram=replace(base.telegram, closed_topic_retention_minutes=10),
    )
    api = FakeApi([])
    store = Store(tmp_path / "retention.sqlite3")
    key = TargetKey.core(controller_core_id="core-1", address="t12")
    store.upsert_telegram_topic_for_target(
        target=key,
        chat_id=-1001,
        thread_id=77,
        name="herdrelay/pi · t12",
        closed=True,
    )
    relay = TelegramService(
        config=app_config,
        store=store,
        client=HcClient(app_config),
        api=api,
    )

    deleted = await relay.topics.cleanup_expired_closed(
        now=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert deleted == ()
    assert api.deleted_topics == []
    assert store.get_telegram_topic_for_target(key) is not None


@pytest.mark.asyncio
async def test_retention_cleanup_isolates_topic_delete_permission_failure(tmp_path):
    base = config()
    app_config = replace(
        base,
        telegram=replace(base.telegram, closed_topic_retention_minutes=10),
    )
    api = FakeApi(
        [],
        delete_errors={
            77: TelegramApiError("Bad Request: CHAT_ADMIN_REQUIRED")
        },
    )
    store = Store(tmp_path / "retention-errors.sqlite3")
    failed = TargetKey.core(controller_core_id="core-1", address="t12")
    successful = TargetKey.core(controller_core_id="core-1", address="t13")
    for key, thread_id in ((failed, 77), (successful, 78)):
        store.upsert_telegram_topic_for_target(
            target=key,
            chat_id=-1001,
            thread_id=thread_id,
            name=f"herdrelay/pi · {key.address}",
            closed=True,
        )
    relay = TelegramService(
        config=app_config,
        store=store,
        client=HcClient(app_config),
        api=api,
    )

    deleted = await relay.topics.cleanup_expired_closed(
        now=datetime.now(UTC) + timedelta(minutes=20)
    )

    assert deleted == (78,)
    assert store.get_telegram_topic_for_target(failed) is not None
    assert store.get_telegram_topic_for_target(successful) is None
    assert [item["message_thread_id"] for item in api.deleted_topics] == [77, 78]

    await relay.topics.cleanup_expired_closed(
        now=datetime.now(UTC) + timedelta(minutes=21)
    )
    assert [item["message_thread_id"] for item in api.deleted_topics] == [77, 78]


def mutation_service(
    tmp_path,
    api,
    *,
    error=None,
    route_core_id="core-1",
    enable_interrupt=False,
    mutation_limit=6,
):
    base = config()
    app_config = replace(
        base,
        telegram=replace(
            base.telegram,
            enable_send=True,
            enable_interrupt=enable_interrupt,
            actor_mutations_per_minute=mutation_limit,
            chat_mutations_per_minute=mutation_limit,
            target_mutations_per_minute=mutation_limit,
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
async def test_orca_topics_follow_exposed_agent_lifecycle(tmp_path):
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="pi",
        status="idle",
        workspace_label="project",
    )

    class FakeOrcaClient(FakeLegacyClient):
        def target_key(self, address):
            return TargetKey.orca(
                instance_id="runtime-1", address=address
            )

    client = FakeOrcaClient([target])
    app_config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            target_absence_grace_seconds=0,
        ),
    )
    api = FakeApi([])
    relay = TelegramService(
        config=app_config,
        store=Store(tmp_path / "orca-lifecycle.sqlite3"),
        client=client,
        api=api,
    )

    await relay.status_relay.legacy_once()
    route = relay.store.get_telegram_topic_for_target(
        client.target_key(target.target)
    )
    assert route is not None
    assert api.created_topics == [
        {"chat_id": -1001, "name": "[orca] project/pi · local/tab:pane"}
    ]

    client.panes = []
    await relay.status_relay.legacy_once()

    assert api.closed_topics == [
        {"chat_id": -1001, "message_thread_id": 101}
    ]


@pytest.mark.asyncio
async def test_orca_transient_missing_snapshot_keeps_topic_open(
    tmp_path, monkeypatch
):
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="claude",
        status="done",
        workspace_label="project",
    )

    class FakeOrcaClient(FakeLegacyClient):
        def target_key(self, address):
            return TargetKey.orca(instance_id="runtime-1", address=address)

    times = iter((100.0, 105.0, 110.0))
    monkeypatch.setattr(
        "herdrelay.telegram_status.monotonic", lambda: next(times)
    )
    client = FakeOrcaClient([target])
    app_config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            target_absence_grace_seconds=60,
        ),
    )
    api = FakeApi([])
    store = Store(tmp_path / "orca-gap.sqlite3")
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )

    await relay.status_relay.legacy_once()
    client.panes = []
    await relay.status_relay.legacy_once()
    client.panes = [target]
    await relay.status_relay.legacy_once()

    route = store.get_telegram_topic_for_target(client.target_key(target.target))
    assert route is not None and route.closed is False
    assert api.closed_topics == []
    assert api.reopened_topics == []


@pytest.mark.asyncio
async def test_orca_sustained_absence_closes_after_grace(tmp_path, monkeypatch):
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="claude",
        status="done",
        workspace_label="project",
    )

    class FakeOrcaClient(FakeLegacyClient):
        def target_key(self, address):
            return TargetKey.orca(instance_id="runtime-1", address=address)

    times = iter((100.0, 105.0, 166.0))
    monkeypatch.setattr(
        "herdrelay.telegram_status.monotonic", lambda: next(times)
    )
    client = FakeOrcaClient([target])
    app_config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            target_absence_grace_seconds=60,
        ),
    )
    api = FakeApi([])
    store = Store(tmp_path / "orca-absence.sqlite3")
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )

    await relay.status_relay.legacy_once()
    client.panes = []
    await relay.status_relay.legacy_once()
    await relay.status_relay.legacy_once()

    route = store.get_telegram_topic_for_target(client.target_key(target.target))
    assert route is not None and route.closed is True
    assert api.closed_topics == [
        {"chat_id": -1001, "message_thread_id": 101}
    ]


@pytest.mark.asyncio
async def test_orca_runtime_rotation_keeps_live_topic_open(tmp_path):
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="pi",
        status="done",
        workspace_label="project",
    )

    class RotatingOrcaClient(FakeLegacyClient):
        runtime_id = "runtime-old"

        def target_key(self, address):
            return TargetKey.orca(instance_id=self.runtime_id, address=address)

    client = RotatingOrcaClient([target])
    app_config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
        ),
    )
    api = FakeApi([])
    store = Store(tmp_path / "orca-rotation.sqlite3")
    relay = TelegramService(
        config=app_config,
        store=store,
        client=client,
        api=api,
    )

    await relay.status_relay.legacy_once()
    client.runtime_id = "runtime-new"
    await relay.status_relay.legacy_once()

    current_key = client.target_key(target.target)
    route = store.get_telegram_topic_for_target(current_key)
    assert route is not None and route.thread_id == "101"
    assert route.route_target == current_key.to_route_value()
    assert api.created_topics == [
        {"chat_id": -1001, "name": "[orca] project/pi · local/tab:pane"}
    ]
    assert api.closed_topics == []
    assert api.reopened_topics == []


@pytest.mark.asyncio
async def test_legacy_tui_topics_status_and_lifecycle_are_reconciled(tmp_path):
    api = FakeApi([])
    target = HerdrTarget(
        target="w1:p1",
        agent_name="pi",
        status="idle",
        workspace_label="project",
    )
    client = FakeLegacyClient([target])
    app_config = AppConfig(
        discord_token="unused",
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            target_absence_grace_seconds=0,
        ),
    )
    relay = TelegramService(
        config=app_config,
        store=Store(tmp_path / "store.sqlite3"),
        client=client,
        api=api,
    )

    await relay.status_relay.legacy_once()

    route = relay.store.get_telegram_topic_for_target(TargetKey.legacy("w1:p1"))
    assert route is not None
    assert api.sent == []

    client.panes = [replace(target, status="blocked")]
    await relay.status_relay.legacy_once()

    assert api.sent[-1]["text"].endswith("\nblocked")

    client.panes = []
    await relay.status_relay.legacy_once()

    route = relay.store.get_telegram_topic_for_target(TargetKey.legacy("w1:p1"))
    assert route is not None and route.closed
    assert api.closed_topics[-1]["message_thread_id"] == int(route.thread_id)


@pytest.mark.asyncio
async def test_legacy_status_restores_and_closes_stale_open_topics(tmp_path):
    api = FakeApi([])
    app_config = AppConfig(
        discord_token="unused",
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            target_absence_grace_seconds=0,
        ),
    )
    store = Store(tmp_path / "store.sqlite3")
    stale_key = TargetKey.legacy("w1:stale")
    store.upsert_telegram_topic_for_target(
        target=stale_key,
        chat_id=-1001,
        thread_id=77,
        name="project/pi · w1:stale",
    )
    relay = TelegramService(
        config=app_config,
        store=store,
        client=FakeLegacyClient([]),
        api=api,
    )

    await relay.status_relay.legacy_once()

    route = store.get_telegram_topic_for_target(stale_key)
    assert route is not None and route.closed
    assert api.closed_topics == [
        {"chat_id": -1001, "message_thread_id": 77}
    ]


@pytest.mark.asyncio
async def test_legacy_tui_prompt_is_claimed_and_audited(tmp_path):
    api = FakeApi([[update(text="continue")]])
    target = HerdrTarget(
        target="w1:p1",
        agent_name="pi",
        status="idle",
        workspace_label="project",
    )
    client = FakeLegacyClient([target])
    app_config = AppConfig(
        discord_token="unused",
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            enable_send=True,
        ),
    )
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_telegram_topic_for_target(
        target=TargetKey.legacy("w1:p1"),
        chat_id=-1001,
        thread_id=77,
        name="project/pi · w1:p1",
    )
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )

    await relay.poll_once()

    assert client.sent == [("w1:p1", "continue")]
    [audit] = store.list_transport_audits()
    assert audit["outcome"] == "succeeded"
    assert audit["target_key"] == TargetKey.legacy("w1:p1").to_storage_key()


@pytest.mark.asyncio
async def test_orca_prompt_is_sent_through_exact_bound_target(tmp_path):
    api = FakeApi([[update(text="continue")]])
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="pi",
        status="done",
        workspace_label="project",
    )

    class FakeOrcaClient:
        def __init__(self):
            self.sent = []

        def list_targets(self):
            return [target]

        def target_key(self, address):
            assert address == target.target
            return TargetKey.orca(
                instance_id="runtime-1", address=target.target
            )

        def resolve_target_key(self, key):
            if key != self.target_key(target.target):
                raise ValueError("target identity mismatch")
            return target

        def send(self, address, text):
            self.sent.append((address, text))
            return MutationReceipt(
                observed_turn=None,
                result={"accepted": True, "request_id": "request-1"},
            )

    app_config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            enable_send=True,
        ),
    )
    store = Store(tmp_path / "orca.sqlite3")
    key = TargetKey.orca(
        instance_id="runtime-1", address="local/tab:pane"
    )
    store.upsert_telegram_topic_for_target(
        target=key,
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    client = FakeOrcaClient()
    relay = TelegramService(
        config=app_config, store=store, client=client, api=api
    )

    await relay.poll_once()

    assert client.sent == [("local/tab:pane", "continue")]
    [audit] = store.list_transport_audits()
    assert audit["target_key"] == key.to_storage_key()
    assert audit["outcome"] == "succeeded"
    assert api.reactions == [
        {"chat_id": -1001, "message_id": 5, "emoji": "👀"}
    ]


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
    assert api.sent == []
    assert api.reactions == [
        {"chat_id": -1001, "message_id": 5, "emoji": "👀"}
    ]
    assert relay.store.get_state(OFFSET_STATE_KEY) == "11"


@pytest.mark.asyncio
async def test_double_slash_command_is_forwarded_to_agent(tmp_path):
    api = FakeApi([[update(text="//reload")]])
    relay, client = mutation_service(tmp_path, api)

    await relay.poll_once()

    assert client.sent == [("t12", "/reload")]


@pytest.mark.asyncio
async def test_prompt_rate_limit_is_enforced_per_actor_chat_and_target(tmp_path):
    api = FakeApi(
        [[update(text="first")], [update(update_id=11, text="second")]]
    )
    relay, client = mutation_service(tmp_path, api, mutation_limit=1)

    await relay.poll_once()
    await relay.poll_once()

    assert client.sent == [("t12", "first")]
    audits = relay.store.list_transport_audits()
    assert [entry["outcome"] for entry in audits] == ["succeeded", "rejected"]
    assert audits[-1]["detail"].startswith("rate limit: actor")
    assert api.sent[-1]["text"] == "Mutation rate limit exceeded."


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
async def test_interrupt_uses_the_same_mutation_rate_limits(tmp_path):
    api = FakeApi(
        [[update(text="first")], [update(update_id=11, text="/interrupt")]]
    )
    relay, client = mutation_service(
        tmp_path, api, enable_interrupt=True, mutation_limit=1
    )
    await relay.poll_once()
    await relay.poll_once()
    callback_data = api.sent[-1]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]

    api.batches.append(
        [callback_update(update_id=12, callback_id="cb-rate", data=callback_data)]
    )
    await relay.poll_once()

    assert client.sent == [("t12", "first")]
    assert client.interrupted == []
    audits = relay.store.list_transport_audits()
    assert [entry["outcome"] for entry in audits] == ["succeeded", "rejected"]
    assert audits[-1]["detail"].startswith("rate limit: actor")
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
