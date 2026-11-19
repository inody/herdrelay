import pytest

from herdrelay.config import load_config


def test_load_config_reads_thread_and_streaming_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
enable_auto_threads: true
enable_streaming: true
thread_parent_channel_id: 123
auto_threads:
  refresh_seconds: 20
streaming:
  mode: hooks
  refresh_seconds: 5
  tail_lines: 40
  initial_tail_lines: 12
  enable_visible_fallback: false
  hook_inbox_path: ~/custom-inbox
  hook_refresh_seconds: 0.5
  hook_max_event_bytes: 12345
  hook_max_event_age_seconds: 600
"""
    )

    config = load_config(config_path)

    assert config.enable_auto_threads is True
    assert config.enable_streaming is True
    assert config.thread_parent_channel_id == 123
    assert config.auto_threads.refresh_seconds == 20
    assert config.streaming.mode == "hooks"
    assert config.streaming.refresh_seconds == 5
    assert config.streaming.tail_lines == 40
    assert config.streaming.initial_tail_lines == 12
    assert config.streaming.enable_visible_fallback is False
    assert config.streaming.hook_inbox_path == "~/custom-inbox"
    assert config.streaming.hook_refresh_seconds == 0.5
    assert config.streaming.hook_max_event_bytes == 12345
    assert config.streaming.hook_max_event_age_seconds == 600


def test_load_config_reads_herdr_core_runtime_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: hc
  core:
    socket_path: /tmp/custom-core.sock
    connect_timeout_seconds: 3
    request_timeout_seconds: 9
    event_idle_timeout_seconds: 45
    require_app_visible: false
    include_hidden_workers: true
    include_connected_machines: true
    allowed_terminal_addresses: [t12, remote/t7]
enable_streaming: true
streaming:
  mode: poll
"""
    )

    config = load_config(config_path)

    assert config.runtime.provider == "herdr-core"
    assert config.runtime.core.socket_path == "/tmp/custom-core.sock"
    assert config.runtime.core.connect_timeout_seconds == 3
    assert config.runtime.core.request_timeout_seconds == 9
    assert config.runtime.core.event_idle_timeout_seconds == 45
    assert config.runtime.core.require_app_visible is False
    assert config.runtime.core.include_hidden_workers is True
    assert config.runtime.core.include_connected_machines is True
    assert config.runtime.core.allowed_terminal_addresses == frozenset(
        {"t12", "remote/t7"}
    )


def test_load_config_reads_deny_by_default_orca_runtime_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """runtime:
  provider: orca
  orca:
    cli_path: /Applications/Orca.app/Contents/Resources/bin/orca
    environment: local
    command_timeout_seconds: 6
    refresh_seconds: 3
    send_wait_seconds: 4
    include_terminal_agents: true
    include_chats: false
    allowed_host_ids: [local]
    allowed_pane_keys: [tab:pane]
    allowed_worktree_ids: [repo::/worktree]
"""
    )

    config = load_config(config_path)

    assert config.runtime.provider == "orca"
    assert config.runtime.orca.cli_path.endswith("/bin/orca")
    assert config.runtime.orca.command_timeout_seconds == 6
    assert config.runtime.orca.refresh_seconds == 3
    assert config.runtime.orca.send_wait_seconds == 4
    assert config.runtime.orca.allowed_host_ids == frozenset({"local"})
    assert config.runtime.orca.allowed_pane_keys == frozenset({"tab:pane"})
    assert config.runtime.orca.allowed_worktree_ids == frozenset(
        {"repo::/worktree"}
    )


def test_load_config_rejects_orca_without_target_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime:\n  provider: orca\n")

    with pytest.raises(ValueError, match="allowed_pane_key or allowed_worktree_id"):
        load_config(config_path)


@pytest.mark.parametrize(
    "setting, message",
    [
        ("enable_watcher: true", "watcher"),
        ("enable_streaming: true\nstreaming:\n  mode: poll", "requires"),
        (
            "transports:\n  telegram:\n    enable_interrupt: true",
            "interrupt",
        ),
    ],
)
def test_load_config_rejects_unimplemented_orca_features(
    tmp_path, monkeypatch, setting, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  provider: orca\n"
        "  orca:\n"
        "    allowed_pane_keys: [tab:pane]\n"
        f"{setting}\n"
    )

    with pytest.raises(ValueError, match=message):
        load_config(config_path)


def test_load_config_allows_normal_send_for_orca(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  provider: orca\n"
        "  orca:\n"
        "    allowed_pane_keys: [tab:pane]\n"
        "transports:\n"
        "  discord:\n"
        "    enable_send: true\n"
    )

    config = load_config(config_path)

    assert config.enable_send is True


def test_load_config_rejects_unknown_runtime_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime:\n  provider: unknown\n")

    try:
        load_config(config_path)
    except ValueError as exc:
        assert "runtime.provider" in str(exc)
    else:
        raise AssertionError("unknown runtime provider should fail")


@pytest.mark.parametrize(
    "body, message",
    [("enable_approve: true", "enable_approve")],
)
def test_load_config_rejects_unimplemented_core_features(
    tmp_path, monkeypatch, body, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runtime:\n  provider: herdr-core\n{body}\n")

    with pytest.raises(ValueError, match=message):
        load_config(config_path)


def test_load_config_accepts_core_hook_streaming(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  provider: herdr-core\n"
        "enable_streaming: true\nstreaming:\n  mode: hooks\n"
    )

    config = load_config(config_path)
    assert config.enable_streaming is True
    assert config.streaming.mode == "hooks"


def test_load_config_accepts_core_event_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  provider: herdr-core\nenable_watcher: true\nenable_stop: true\n"
    )

    config = load_config(config_path)
    assert config.enable_watcher is True
    assert config.enable_stop is True



def test_database_default_prefers_herdrelay_and_preserves_legacy_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "token")

    assert load_config(tmp_path / "missing.yaml").database_path == "herdrelay.sqlite3"

    (tmp_path / "herdr-discord-bridge.sqlite3").touch()
    assert (
        load_config(tmp_path / "missing.yaml").database_path
        == "herdr-discord-bridge.sqlite3"
    )

    (tmp_path / "herdrelay.sqlite3").touch()
    assert load_config(tmp_path / "missing.yaml").database_path == "herdrelay.sqlite3"


def test_load_config_can_disable_discord_without_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("transports:\n  discord:\n    enabled: false\n")

    config = load_config(config_path)

    assert config.discord.enabled is False
    assert config.discord_token == ""


def test_load_config_accepts_grouped_storage_relay_and_discord(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """storage:
  database_path: grouped.sqlite3
relay:
  max_message_chars: 321
  enable_streaming: true
  dangerous_text_blocklist: [blocked]
  streaming:
    hook_inbox_path: /tmp/grouped-inbox
transports:
  discord:
    enabled: true
    allowed_guild_ids: [1]
    allowed_channel_ids: [2]
    allowed_user_ids: [3]
    thread_parent_channel_id: 2
    enable_send: true
    auto_threads:
      refresh_seconds: 7
"""
    )

    config = load_config(config_path)

    assert config.database_path == "grouped.sqlite3"
    assert config.max_message_chars == 321
    assert config.enable_streaming is True
    assert config.streaming.hook_inbox_path == "/tmp/grouped-inbox"
    assert config.dangerous_text_blocklist == ("blocked",)
    assert config.allowed_guild_ids == frozenset({1})
    assert config.allowed_channel_ids == frozenset({2})
    assert config.allowed_user_ids == frozenset({3})
    assert config.thread_parent_channel_id == 2
    assert config.enable_send is True
    assert config.auto_threads.refresh_seconds == 7


def test_load_config_merges_file_managed_core_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    (tmp_path / "targets.txt").write_text("# local policy\nt2\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """runtime:
  provider: herdr-core
  herdr_core:
    allowed_terminal_addresses: [t1]
    allowed_terminal_addresses_file: targets.txt
"""
    )

    config = load_config(config_path)

    assert config.runtime.core.allowed_terminal_addresses == frozenset({"t1", "t2"})
    assert config.runtime.core.allowed_terminal_addresses_file == str(
        (tmp_path / "targets.txt").resolve()
    )


def test_load_config_accepts_grouped_runtime_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """runtime:
  provider: herdr-core
  herdr_tui:
    cli_path: /opt/bin/herdr
    command_timeout_seconds: 9
  herdr_core:
    socket_path: /tmp/core.sock
    allowed_terminal_addresses: [t1]
"""
    )

    config = load_config(config_path)

    assert config.runtime.provider == "herdr-core"
    assert config.runtime.tui.cli_path == "/opt/bin/herdr"
    assert config.herdr.command_timeout_seconds == 9
    assert config.runtime.core.socket_path == "/tmp/core.sock"
    assert config.runtime.core.allowed_terminal_addresses == frozenset({"t1"})


def test_load_config_keeps_discord_enabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")

    config = load_config(tmp_path / "missing.yaml")

    assert config.discord.enabled is True


def test_load_config_reads_enabled_telegram_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: herdr-core
transports:
  telegram:
    enabled: true
    mode: forum
    allowed_chat_ids: [-100123]
    allowed_user_ids: [42]
    forum_chat_id: -100123
    statuses: [blocked]
    enable_send: true
    enable_interrupt: true
    confirmation_ttl_seconds: 90
    actor_mutations_per_minute: 2
    chat_mutations_per_minute: 3
    target_mutations_per_minute: 4
    poll_timeout_seconds: 20
    request_timeout_seconds: 30
    reconnect_delay_seconds: 2
"""
    )

    config = load_config(config_path)

    assert config.telegram.enabled is True
    assert config.telegram.token == "telegram-secret"
    assert config.telegram.allowed_chat_ids == frozenset({-100123})
    assert config.telegram.allowed_user_ids == frozenset({42})
    assert config.telegram.forum_chat_id == -100123
    assert config.telegram.statuses == ("blocked",)
    assert config.telegram.enable_send is True
    assert config.telegram.enable_interrupt is True
    assert config.telegram.confirmation_ttl_seconds == 90
    assert config.telegram.actor_mutations_per_minute == 2
    assert config.telegram.chat_mutations_per_minute == 3
    assert config.telegram.target_mutations_per_minute == 4
    assert config.telegram.poll_timeout_seconds == 20
    assert config.telegram.request_timeout_seconds == 30
    assert "telegram-secret" not in repr(config.telegram)


def test_load_config_allows_telegram_with_legacy_tui(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """runtime:
  provider: herdr-tui
transports:
  telegram:
    enabled: true
    allowed_chat_ids: [-1]
    allowed_user_ids: [2]
    forum_chat_id: -1
"""
    )

    config = load_config(config_path)

    assert config.runtime.provider == "herdr-tui"
    assert config.telegram.enabled is True


@pytest.mark.parametrize(
    "runtime, telegram, message",
    [
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n    forum_chat_id: -1",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            "herdr-core",
            "allowed_user_ids: [2]\n    forum_chat_id: -1",
            "allowed_chat_id",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    forum_chat_id: -1",
            "allowed_user_id",
        ),
        (
            "herdr-core",
            "mode: direct\n    allowed_chat_ids: [-1]\n"
            "    allowed_user_ids: [2]\n    forum_chat_id: -1",
            "only 'forum'",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n    forum_chat_id: -2",
            "forum_chat_id must be in allowed_chat_ids",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n"
            "    forum_chat_id: -1\n    statuses: [working]",
            "only blocked and done",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n"
            "    forum_chat_id: -1\n    confirmation_ttl_seconds: 5",
            "confirmation_ttl_seconds",
        ),
    ],
)
def test_load_config_rejects_unsafe_telegram_settings(
    tmp_path, monkeypatch, runtime, telegram, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    if "TELEGRAM_BOT_TOKEN" not in message:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
runtime:
  provider: {runtime}
transports:
  telegram:
    enabled: true
    {telegram}
"""
    )

    with pytest.raises(ValueError, match=message):
        load_config(config_path)



def test_load_config_rejects_invalid_core_runtime_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: herdr-core
  core:
    request_timeout_seconds: 0
"""
    )

    with pytest.raises(ValueError, match="request_timeout_seconds"):
        load_config(config_path)

    config_path.write_text(
        """
runtime:
  provider: herdr-core
  core:
    event_idle_timeout_seconds: 15
"""
    )
    with pytest.raises(ValueError, match="15s heartbeat"):
        load_config(config_path)

    config_path.write_text(
        """
runtime:
  provider: herdr-core
  core:
    allowed_terminal_addresses: t12
"""
    )
    with pytest.raises(ValueError, match="YAML lists"):
        load_config(config_path)



def test_load_config_rejects_unknown_streaming_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("streaming:\n  mode: unknown\n")

    try:
        load_config(config_path)
    except ValueError as exc:
        assert "streaming.mode" in str(exc)
    else:
        raise AssertionError("unknown streaming mode should fail")


def test_load_config_allows_status_only_watcher_without_resubscribe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
watcher:
  statuses: ["blocked"]
  resubscribe_interval_seconds: 0
  include_output: false
  blocked_notification_delay_seconds: 2
"""
    )

    config = load_config(config_path)

    assert config.watcher.statuses == ("blocked",)
    assert config.watcher.resubscribe_interval_seconds == 0
    assert config.watcher.include_output is False
    assert config.watcher.blocked_notification_delay_seconds == 2


def test_load_config_accepts_empty_watcher_statuses(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("watcher:\n  statuses: []\n")

    assert load_config(config_path).watcher.statuses == ()


def test_load_config_parses_deny_and_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
enable_stop: true
approval:
  codex:
    method: send_text_enter
    text: "y"
deny:
  claude:
    method: send_keys
    keys: ["Escape"]
stop:
  method: send_keys
  keys: ["C-c"]
"""
    )

    config = load_config(config_path)

    assert config.enable_stop is True
    assert config.approval["codex"].text == "y"
    assert config.deny["claude"].keys == ("Escape",)
    assert config.stop is not None
    assert config.stop.method == "send_keys"
    assert config.stop.keys == ("C-c",)


def test_load_config_defaults_stop_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config = load_config(tmp_path / "empty.yaml")

    assert config.enable_stop is False
    assert config.deny == {}
    assert config.stop is None
