from herdr_discord_bridge.models import (
    PendingQuestion,
    QuestionOption,
    TransportAuditEntry,
)
from herdr_discord_bridge.store import Store


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
