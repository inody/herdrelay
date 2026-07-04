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
  refresh_seconds: 5
  tail_lines: 40
"""
    )

    config = load_config(config_path)

    assert config.enable_auto_threads is True
    assert config.enable_streaming is True
    assert config.thread_parent_channel_id == 123
    assert config.auto_threads.refresh_seconds == 20
    assert config.streaming.refresh_seconds == 5
    assert config.streaming.tail_lines == 40


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
