import sqlite3

from herdrelay.domain import TargetKey
from herdrelay.models import (
    PendingQuestion,
    QuestionOption,
    TransportAuditEntry,
)
from herdrelay.store import Store


def test_channel_binding_roundtrip(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=None,
        herdr_target="1-1",
        label="main",
        created_by=10,
    )

    binding = store.get_binding(guild_id=1, channel_id=2, thread_id=None)
    assert binding is not None
    assert binding.herdr_target == "1-1"
    assert binding.label == "main"


def test_thread_binding_overrides_channel_binding(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=None,
        herdr_target="1-1",
        label=None,
        created_by=10,
    )
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=3,
        herdr_target="1-2",
        label=None,
        created_by=10,
    )

    assert store.get_binding(guild_id=1, channel_id=2, thread_id=3).herdr_target == "1-2"
    assert store.get_binding(guild_id=1, channel_id=2, thread_id=4).herdr_target == "1-1"


def test_upsert_replaces_existing_null_thread_binding(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    for target in ("1-1", "1-2"):
        store.upsert_binding(
            guild_id=1,
            channel_id=2,
            thread_id=None,
            herdr_target=target,
            label=None,
            created_by=10,
        )

    bindings = store.list_bindings(guild_id=1)
    assert len(bindings) == 1
    assert bindings[0].herdr_target == "1-2"


def test_telegram_confirmation_is_actor_location_bound_expiring_and_one_time(tmp_path):
    store = Store(tmp_path / "confirmation.sqlite3")
    store.create_telegram_confirmation(
        token="secret",
        actor_id=42,
        chat_id=-1001,
        thread_id=77,
        core_id="core-1",
        terminal_address="t1",
        action="interrupt",
        expires_at=200,
    )

    assert store.consume_telegram_confirmation(
        token="secret",
        actor_id=9,
        chat_id=-1001,
        thread_id=77,
        action="interrupt",
        now=100,
    ) is None
    confirmation = store.consume_telegram_confirmation(
        token="secret",
        actor_id=42,
        chat_id=-1001,
        thread_id=77,
        action="interrupt",
        now=100,
    )
    assert confirmation is not None
    assert confirmation.core_id == "core-1"
    assert confirmation.terminal_address == "t1"
    assert store.consume_telegram_confirmation(
        token="secret",
        actor_id=42,
        chat_id=-1001,
        thread_id=77,
        action="interrupt",
        now=100,
    ) is None

    store.create_telegram_confirmation(
        token="expired",
        actor_id=42,
        chat_id=-1001,
        thread_id=77,
        core_id="core-1",
        terminal_address="t1",
        action="interrupt",
        expires_at=99,
    )
    assert store.consume_telegram_confirmation(
        token="expired",
        actor_id=42,
        chat_id=-1001,
        thread_id=77,
        action="interrupt",
        now=100,
    ) is None


def test_transport_mutation_claim_is_atomic_and_single_use(tmp_path):
    store = Store(tmp_path / "audit.sqlite3")
    entry = TransportAuditEntry(
        transport="telegram",
        actor_id="42",
        container_id="-1001",
        conversation_id="77",
        action="send_text",
        target_key="core-1/t1",
        payload_preview="continue",
        outcome="attempt",
    )

    audit_id = store.claim_transport_mutation(
        event_key="telegram:update:10", entry=entry
    )
    duplicate = store.claim_transport_mutation(
        event_key="telegram:update:10", entry=entry
    )

    assert isinstance(audit_id, int)
    assert duplicate is None
    [attempt] = store.list_transport_audits()
    assert attempt["outcome"] == "attempt"
    assert attempt["target_key"] == "core-1/t1"

    store.finish_transport_audit(
        audit_id, outcome="succeeded", detail="accepted", observed_turn=4
    )
    [finished] = store.list_transport_audits()
    assert finished["outcome"] == "succeeded"
    assert finished["observed_turn"] == 4
    assert finished["detail"] == "accepted"


def test_existing_core_topic_schema_is_migrated_to_normalized_route(tmp_path):
    path = tmp_path / "store.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_topics (
              core_id TEXT NOT NULL,
              terminal_address TEXT NOT NULL,
              chat_id TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              name TEXT NOT NULL,
              closed INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(core_id, terminal_address),
              UNIQUE(chat_id, thread_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO telegram_topics VALUES (
              'core-1', 't1', '-1001', '77', 'project/pi', 0, 'now', 'now'
            )
            """
        )

    store = Store(path)

    route = store.get_telegram_topic_for_target(
        TargetKey.core(controller_core_id="core-1", address="t1")
    )
    assert route is not None
    assert route.thread_id == "77"
    assert TargetKey.from_route_value(route.route_target) == TargetKey.core(
        controller_core_id="core-1", address="t1"
    )


def test_telegram_topic_route_upsert_and_close(tmp_path):
    store = Store(tmp_path / "store.sqlite3")

    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t1",
        chat_id=-1001,
        thread_id=77,
        name="project/pi",
    )
    route = store.get_telegram_topic(core_id="core-1", terminal_address="t1")

    assert route is not None
    assert route.chat_id == "-1001"
    assert route.thread_id == "77"
    assert route.closed is False
    by_destination = store.get_telegram_topic_by_destination(
        chat_id=-1001, thread_id=77
    )
    assert by_destination == route

    store.set_telegram_topic_closed(
        core_id="core-1", terminal_address="t1", closed=True
    )
    closed = store.get_telegram_topic(core_id="core-1", terminal_address="t1")
    assert closed is not None and closed.closed is True

    store.upsert_telegram_topic(
        core_id="core-1",
        terminal_address="t1",
        chat_id=-1001,
        thread_id=77,
        name="project/pi done",
        closed=False,
    )
    updated = store.get_telegram_topic(core_id="core-1", terminal_address="t1")
    assert updated is not None
    assert updated.name == "project/pi done"
    assert updated.closed is False



def test_state_roundtrip(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    assert store.get_state("dashboard_message_id") is None
    store.set_state("dashboard_message_id", "123")
    store.set_state("dashboard_message_id", "456")

    assert store.get_state("dashboard_message_id") == "456"


def test_event_key_prefix_matches_legacy_bucketed_keys(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    assert store.mark_event_seen("w7:p1:done:abc123:5943680")

    assert store.has_event_key("w7:p1:done:abc123:5943680")
    assert not store.has_event_key("w7:p1:done:abc123")
    assert store.has_event_key_prefix("w7:p1:done:abc123")
    assert not store.has_event_key_prefix("w7:p1:blocked:abc123")


def test_pending_question_roundtrip_and_conditional_clear(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    question = PendingQuestion(
        pane_id="w7:p5",
        event_id="question-1",
        prompt="Choose",
        options=(QuestionOption("One"), QuestionOption("Two", "Second")),
    )

    store.upsert_pending_question(question)

    assert store.get_pending_question("w7:p5") == question
    store.clear_pending_question("w7:p5", event_id="different")
    assert store.get_pending_question("w7:p5") == question
    store.clear_pending_question("w7:p5", event_id="question-1")
    assert store.get_pending_question("w7:p5") is None


def test_agent_thread_target_migration_updates_binding_atomically(tmp_path):
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_agent_thread(
        pane_id="t1", thread_id=111, guild_id=1, alias="project"
    )
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=111,
        herdr_target="t1",
        label="project",
        created_by=1,
    )
    core_target = (
        '{"address":"t1","controller_core_id":"core-1",'
        '"origin_core_id":null,"runtime":"herdr-core","v":1}'
    )

    assert store.migrate_agent_thread_target(
        old_target="t1", new_target=core_target
    )

    assert store.get_agent_thread("t1") is None
    assert store.get_agent_thread(core_target) == "111"
    binding = store.get_binding(guild_id=1, channel_id=2, thread_id=111)
    assert binding is not None
    assert binding.herdr_target == core_target


def test_agent_thread_upsert_and_get(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    assert store.get_agent_thread("w7:p5") is None

    store.upsert_agent_thread(pane_id="w7:p5", thread_id=111, guild_id=1, alias="herdr/claude")

    assert store.get_agent_thread("w7:p5") == "111"

    store.upsert_agent_thread(pane_id="w7:p5", thread_id=222, guild_id=1, alias="herdr/claude")

    assert store.get_agent_thread("w7:p5") == "222"


def test_pending_questions_for_closed_panes_are_cleared(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    for pane_id in ("w1:p1", "w2:p1"):
        store.upsert_pending_question(
            PendingQuestion(
                pane_id=pane_id,
                event_id=f"question-{pane_id}",
                prompt="Choose",
                options=(QuestionOption("One"),),
            )
        )

    store.clear_pending_questions_except({"w1:p1"})

    assert store.get_pending_question("w1:p1") is not None
    assert store.get_pending_question("w2:p1") is None


def test_list_pending_questions_returns_every_pane(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    for pane_id in ("w1:p1", "w2:p1"):
        store.upsert_pending_question(
            PendingQuestion(
                pane_id=pane_id,
                event_id=f"question-{pane_id}",
                prompt="Choose",
                options=(QuestionOption("One"),),
            )
        )

    assert {q.pane_id for q in store.list_pending_questions()} == {"w1:p1", "w2:p1"}
