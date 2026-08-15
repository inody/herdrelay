from herdr_discord_bridge.config import AppConfig, WatcherConfig
from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.watcher import (
    AgentStatusEvent,
    blocked_mention_prefix,
    build_agent_status_subscriptions,
    event_dedupe_key,
    event_state_marker,
    parse_agent_status_event,
    resolve_socket_path,
    should_resubscribe,
)


def test_parse_agent_status_event_from_nested_event_payload():
    event = parse_agent_status_event(
        {
            "event": {
                "type": "pane.agent_status_changed",
                "payload": {
                    "pane_id": "w7:p2",
                    "new_status": "BLOCKED",
                    "previous_status": "working",
                    "agent_name": "codex",
                },
            }
        }
    )

    assert event == AgentStatusEvent(
        pane_id="w7:p2",
        status="blocked",
        previous_status="working",
        agent_name="codex",
        raw={
            "event": {
                "type": "pane.agent_status_changed",
                "payload": {
                    "pane_id": "w7:p2",
                    "new_status": "BLOCKED",
                    "previous_status": "working",
                    "agent_name": "codex",
                },
            }
        },
    )


def test_parse_agent_status_event_from_pane_snapshot():
    event = parse_agent_status_event(
        {
            "type": "pane.agent_status_changed",
            "pane": {"pane_id": "w1:p1", "agent_status": "done"},
            "agent": {"name": "claude"},
        }
    )

    assert event is not None
    assert event.pane_id == "w1:p1"
    assert event.status == "done"
    assert event.agent_name == "claude"


def test_parse_agent_status_event_ignores_other_events():
    assert parse_agent_status_event({"type": "pane.created", "pane_id": "w1:p1"}) is None


def test_dedupe_key_is_stable_across_time():
    event = AgentStatusEvent(pane_id="w1:p1", status="blocked")

    first = event_dedupe_key(event, "tail")
    second = event_dedupe_key(event, "tail")

    assert first == second
    assert first == "w1:p1:blocked:0c62f876ef1dea83"


def test_event_state_marker_uses_state_change_sequence_without_reading_output():
    event = AgentStatusEvent(pane_id="w1:p1", status="blocked")

    marker = event_state_marker(StateMarkerClient(), event)

    assert marker == "state-change:42"


def test_find_binding_for_target_prefers_thread_binding(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=None,
        herdr_target="w1:p1",
        label="channel",
        created_by=10,
    )
    store.upsert_binding(
        guild_id=1,
        channel_id=2,
        thread_id=3,
        herdr_target="w1:p1",
        label="thread",
        created_by=10,
    )

    binding = store.find_binding_for_target("w1:p1")

    assert binding is not None
    assert binding.thread_id == "3"


def test_resolve_socket_path_uses_config_first():
    assert resolve_socket_path(AppConfig(discord_token="token", herdr_socket_path="/tmp/herdr.sock")) == (
        "/tmp/herdr.sock"
    )


def test_build_agent_status_subscriptions_uses_pane_and_status_filters():
    config = AppConfig(discord_token="token")

    subscriptions = build_agent_status_subscriptions(FakeHerdrClient(), config)

    assert subscriptions == [
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1", "agent_status": "blocked"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p1", "agent_status": "done"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p2", "agent_status": "blocked"},
        {"type": "pane.agent_status_changed", "pane_id": "w1:p2", "agent_status": "done"},
    ]


def test_should_resubscribe_after_configured_interval(monkeypatch):
    config = AppConfig(discord_token="token")
    monkeypatch.setattr("herdr_discord_bridge.watcher.time.monotonic", lambda: 500.0)

    assert should_resubscribe(199.9, config)
    assert not should_resubscribe(250.1, config)


def test_should_resubscribe_can_be_disabled(monkeypatch):
    config = AppConfig(
        discord_token="token",
        watcher=WatcherConfig(
            resubscribe_interval_seconds=0,
        ),
    )
    monkeypatch.setattr("herdr_discord_bridge.watcher.time.monotonic", lambda: 1000.0)

    assert not should_resubscribe(0, config)


def test_blocked_mention_prefix_lists_allowed_users():
    config = AppConfig(discord_token="token", allowed_user_ids=frozenset({111, 222}))

    prefix = blocked_mention_prefix(config)

    assert "<@111>" in prefix
    assert "<@222>" in prefix
    assert prefix.endswith("\n")


def test_blocked_mention_prefix_empty_when_no_users():
    config = AppConfig(discord_token="token")

    assert blocked_mention_prefix(config) == ""


class StateMarkerClient:
    def resolve_target(self, target):
        return HerdrTarget(target=target, raw={"state_change_seq": 42})


class FakeHerdrClient:
    def list_targets(self):
        return [
            HerdrTarget(target="w1:p1"),
            HerdrTarget(target="w1:p2"),
        ]
