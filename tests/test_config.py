import pytest

from herdrelay.config import effective_general_provider, load_config


@pytest.mark.parametrize(
    ("runtime", "expected"),
    (("orca", "orca-bot"), ("herdr-core", "router"), ("herdr-tui", "router")),
)
def test_general_auto_selects_provider_for_runtime(
    tmp_path, monkeypatch, runtime, expected
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
runtime:
  provider: {runtime}
relay:
  general:
    enabled: true
    provider: auto
    model:
      provider: command
      argv: [router]
    orca_bot:
      bot_id: bot-1
      gateway_worktree: folder:bot
      gateway_command: run-gateway
transports:
  discord:
    enabled: true
    enable_send: true
    allowed_user_ids: [42]
"""
    )

    config = load_config(config_path)

    assert effective_general_provider(config) == expected


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
    target_exposure:
      mode: selected
      addresses: [t12, remote/t7]
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
    assert config.runtime.core.target_exposure.mode == "selected"
    assert config.runtime.core.target_exposure.addresses == frozenset(
        {"t12", "remote/t7"}
    )


def test_load_config_defaults_core_to_all_eligible_targets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime:\n  provider: herdr-core\n")

    config = load_config(config_path)

    assert config.runtime.core.target_exposure.mode == "all-eligible"
    assert not config.runtime.core.target_exposure.addresses


def test_load_config_reads_selected_orca_runtime_settings(
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
    host_ids: [local]
    target_exposure:
      mode: selected
      pane_keys: [tab:pane]
      worktree_ids: [repo::/worktree]
"""
    )

    config = load_config(config_path)

    assert config.runtime.provider == "orca"
    assert config.runtime.orca.cli_path.endswith("/bin/orca")
    assert config.runtime.orca.command_timeout_seconds == 6
    assert config.runtime.orca.refresh_seconds == 3
    assert config.runtime.orca.send_wait_seconds == 4
    assert config.runtime.orca.host_ids == frozenset({"local"})
    assert config.runtime.orca.target_exposure.mode == "selected"
    assert config.runtime.orca.target_exposure.pane_keys == frozenset(
        {"tab:pane"}
    )
    assert config.runtime.orca.target_exposure.worktree_ids == frozenset(
        {"repo::/worktree"}
    )


def test_load_config_defaults_orca_to_all_eligible_local_agents(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("runtime:\n  provider: orca\n")

    config = load_config(config_path)

    assert config.runtime.orca.host_ids == frozenset({"local"})
    assert config.runtime.orca.target_exposure.mode == "all-eligible"
    assert not config.runtime.orca.target_exposure.pane_keys
    assert not config.runtime.orca.target_exposure.worktree_ids


def test_load_config_preserves_legacy_empty_core_allowlist_as_none(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  provider: herdr-core\n"
        "  herdr_core:\n"
        "    allowed_terminal_addresses: []\n"
    )

    config = load_config(config_path)

    assert config.runtime.core.target_exposure.mode == "none"


def test_load_config_preserves_legacy_orca_allowlist_as_selected(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  provider: orca\n"
        "  orca:\n"
        "    allowed_host_ids: [local]\n"
        "    allowed_pane_keys: [tab:pane]\n"
    )

    config = load_config(config_path)

    assert config.runtime.orca.host_ids == frozenset({"local"})
    assert config.runtime.orca.target_exposure.mode == "selected"
    assert config.runtime.orca.target_exposure.pane_keys == frozenset(
        {"tab:pane"}
    )


def test_load_config_preserves_legacy_empty_orca_allowlist_as_none(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  provider: orca\n"
        "  orca:\n"
        "    allowed_pane_keys: []\n"
        "    allowed_worktree_ids: []\n"
    )

    config = load_config(config_path)

    assert config.runtime.orca.target_exposure.mode == "none"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "runtime:\n  provider: orca\n  orca:\n"
            "    target_exposure:\n      mode: selected\n",
            "requires exact selectors",
        ),
        (
            "runtime:\n  provider: orca\n  orca:\n"
            "    target_exposure:\n      mode: all-eligible\n"
            "      pane_keys: [tab:pane]\n",
            "cannot include selectors",
        ),
        (
            "runtime:\n  provider: orca\n  orca:\n"
            "    target_exposure:\n      mode: all-eligible\n"
            "    allowed_pane_keys: [tab:pane]\n",
            "conflicts",
        ),
    ],
)
def test_load_config_rejects_ambiguous_target_exposure(
    tmp_path, monkeypatch, body, message
):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(body)

    with pytest.raises(ValueError, match=message):
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
        "    target_exposure:\n"
        "      mode: all-eligible\n"
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
        "    target_exposure:\n"
        "      mode: all-eligible\n"
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
    [("enable_questions: true", "question answering")],
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
    assert config.enable_interrupt is True



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
    target_exposure:
      mode: selected
      addresses: [t1]
      addresses_file: targets.txt
"""
    )

    config = load_config(config_path)

    assert config.runtime.core.target_exposure.addresses == frozenset(
        {"t1", "t2"}
    )
    assert config.runtime.core.target_exposure.addresses_file == str(
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
    assert config.runtime.core.target_exposure.mode == "selected"
    assert config.runtime.core.target_exposure.addresses == frozenset({"t1"})


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
    target_absence_grace_seconds: 45
    closed_topic_retention_minutes: 10
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
    assert config.telegram.target_absence_grace_seconds == 45
    assert config.telegram.closed_topic_retention_minutes == 10
    assert config.telegram.closed_topic_retention_days is None
    assert "telegram-secret" not in repr(config.telegram)


def test_load_config_reads_general_router_model(tmp_path, monkeypatch):
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
    allowed_chat_ids: [-1001]
    allowed_user_ids: [42]
    forum_chat_id: -1001
    enable_send: true
    general_router:
      enabled: true
      activation: all-text
      execution_policy: auto-unique
      context_ttl_seconds: 1800
      model:
        provider: openai-compatible
        model: router-small
        base_url: http://127.0.0.1:11434/v1
        timeout_seconds: 4
        max_output_bytes: 8192
"""
    )

    config = load_config(config_path)

    router = config.telegram.general_router
    assert router.enabled is True
    assert router.activation == "all-text"
    assert router.execution_policy == "auto-unique"
    assert router.context_ttl_seconds == 1800
    assert router.model.provider == "openai-compatible"
    assert router.model.model == "router-small"
    assert router.model.base_url == "http://127.0.0.1:11434/v1"
    assert router.model.timeout_seconds == 4
    assert router.model.max_output_bytes == 8192


@pytest.mark.parametrize("runtime_provider", ("herdr-core", "herdr-tui", "orca"))
def test_load_config_allows_general_router_with_every_runtime(
    tmp_path, monkeypatch, runtime_provider
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
runtime:
  provider: {runtime_provider}
transports:
  discord:
    enabled: false
  telegram:
    enabled: true
    allowed_chat_ids: [-1]
    allowed_user_ids: [2]
    forum_chat_id: -1
    enable_send: true
    general_router:
      enabled: true
      provider: router
      model:
        provider: command
        argv: [router]
"""
    )

    config = load_config(config_path, require_discord_token=False)

    assert config.runtime.provider == runtime_provider
    assert config.telegram.general_router.provider == "router"


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
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n"
            "    forum_chat_id: -1\n    closed_topic_retention_days: 0",
            "closed_topic_retention_days",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n"
            "    forum_chat_id: -1\n    closed_topic_retention_minutes: 10\n"
            "    closed_topic_retention_days: 1",
            "minutes or legacy days",
        ),
        (
            "herdr-core",
            "allowed_chat_ids: [-1]\n    allowed_user_ids: [2]\n"
            "    forum_chat_id: -1\n    target_absence_grace_seconds: -1",
            "target_absence_grace_seconds",
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


def test_general_router_requires_send_and_explicit_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    base = """
runtime:
  provider: herdr-core
transports:
  telegram:
    enabled: true
    allowed_chat_ids: [-1]
    allowed_user_ids: [2]
    forum_chat_id: -1
    general_router:
      enabled: true
      model:
        provider: command
        node_path: /opt/node/bin/node
        pass_env: [ROUTER_TOKEN]
        extensions: [/tmp/provider.ts]
        argv: [router]
"""
    config_path.write_text(base)
    with pytest.raises(ValueError, match="requires enable_send"):
        load_config(config_path)

    config_path.write_text(base.replace("enabled: true\n    allowed_chat", "enabled: true\n    enable_send: true\n    allowed_chat"))
    config = load_config(config_path)
    assert config.telegram.general_router.model.node_path == "/opt/node/bin/node"
    assert config.telegram.general_router.model.pass_env == ("ROUTER_TOKEN",)
    assert config.telegram.general_router.model.extensions == ("/tmp/provider.ts",)
    assert config.telegram.general_router.model.argv == ("router",)


def test_load_config_reads_voice_input_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: orca
transports:
  discord:
    enabled: false
  telegram:
    enabled: true
    allowed_chat_ids: [-1]
    allowed_user_ids: [2]
    forum_chat_id: -1
    enable_send: true
    general_router:
      enabled: true
      model:
        provider: command
        argv: [router]
    voice_input:
      enabled: true
      scope: general-and-bound
      max_duration_seconds: 90
      max_file_bytes: 5000000
      transcription:
        provider: mlx-whisper
        model: mlx-community/whisper-large-v3-turbo
        language: smart
        python_path: /opt/voice/bin/python
        pass_env: [HF_TOKEN]
        timeout_seconds: 60
"""
    )

    config = load_config(config_path)

    voice = config.telegram.voice_input
    assert voice.enabled is True
    assert voice.scope == "general-and-bound"
    assert voice.max_duration_seconds == 90
    assert voice.max_file_bytes == 5_000_000
    assert voice.transcription.provider == "mlx-whisper"
    assert voice.transcription.model == "mlx-community/whisper-large-v3-turbo"
    assert voice.transcription.python_path == "/opt/voice/bin/python"
    assert voice.transcription.pass_env == ("HF_TOKEN",)


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


def test_legacy_enable_approve_maps_to_structured_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: herdr-tui
enable_approve: true
approval:
  claude:
    method: send_keys
    keys: ["Enter"]
"""
    )

    config = load_config(config_path)

    assert config.enable_questions is True


def test_legacy_tui_enable_stop_no_longer_enables_pane_close(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("enable_stop: true\n")

    assert load_config(config_path).enable_interrupt is False


def test_load_config_defaults_optional_discord_actions_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config = load_config(tmp_path / "empty.yaml")

    assert config.enable_questions is False
    assert config.enable_interrupt is False


def test_load_config_accepts_orca_bot_general_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: orca
transports:
  telegram:
    enabled: true
    forum_chat_id: -1001
    allowed_chat_ids: [-1001]
    allowed_user_ids: [42]
    enable_send: true
    general_router:
      enabled: true
      provider: orca-bot
      orca_bot:
        bot_id: bot-1
        queue_path: ~/.cache/test-gateway
        gateway_worktree: folder:bot-home
        gateway_command: run-gateway
"""
    )

    config = load_config(config_path, require_discord_token=False)

    assert config.telegram.general_router.provider == "orca-bot"
    assert config.telegram.general_router.orca_bot.bot_id == "bot-1"
    assert config.telegram.general_router.orca_bot.queue_path.endswith(
        "/.cache/test-gateway"
    )


def test_load_config_rejects_orca_bot_provider_for_non_orca_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  provider: herdr-core
transports:
  telegram:
    enabled: true
    forum_chat_id: -1001
    allowed_chat_ids: [-1001]
    allowed_user_ids: [42]
    enable_send: true
    general_router:
      enabled: true
      provider: orca-bot
      orca_bot:
        bot_id: bot-1
        gateway_worktree: folder:bot-home
        gateway_command: run-gateway
"""
    )

    with pytest.raises(ValueError, match="requires runtime.provider=orca"):
        load_config(config_path, require_discord_token=False)
