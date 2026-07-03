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


def test_state_roundtrip(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    assert store.get_state("dashboard_message_id") is None
    store.set_state("dashboard_message_id", "123")
    store.set_state("dashboard_message_id", "456")

    assert store.get_state("dashboard_message_id") == "456"


def test_notification_message_roundtrip(tmp_path):
    store = Store(tmp_path / "bridge.sqlite3")

    assert store.get_notification_target("123") is None
    store.add_notification_message(message_id="123", herdr_target="w7:p3", kind="blocked")

    assert store.get_notification_target("123") == "w7:p3"
