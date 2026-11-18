import pytest

from herdr_discord_bridge.config import load_config


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
    [
        ("enable_approve: true", "enable_approve"),
        ("enable_streaming: true", "streaming.mode=poll"),
    ],
)
def test_load_config_rejects_unimplemented_core_features(
    tmp_path, monkeypatch, body, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runtime:\n  provider: herdr-core\n{body}\n")

    with pytest.raises(ValueError, match=message):
        load_config(config_path)


def test_load_config_accepts_core_event_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n  provider: herdr-core\nenable_watcher: true\nenable_stop: true\n"
    )

    config = load_config(config_path)
    assert config.enable_watcher is True
    assert config.enable_stop is True



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
    assert config.telegram.poll_timeout_seconds == 20
    assert config.telegram.request_timeout_seconds == 30
    assert "telegram-secret" not in repr(config.telegram)


@pytest.mark.parametrize(
    "runtime, telegram, message",
    [
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]",
            "TELEGRAM_BOT_TOKEN",
        ),
        (
            "herdr-tui",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]",
            "requires runtime.provider",
        ),
        ("herdr-core", "allowed_user_ids: [2]", "allowed_chat_id"),
        ("herdr-core", "allowed_chat_ids: [-1]", "allowed_user_id"),
        (
            "herdr-core",
            "mode: direct\n    allowed_chat_ids: [-1]\n    allowed_user_ids: [2]",
            "only 'forum'",
        ),
    ],
)
def test_load_config_rejects_unsafe_telegram_settings(
    tmp_path, monkeypatch, runtime, telegram, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
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
