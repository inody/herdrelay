from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class HerdrCliConfig:
    cli_path: str = "herdr"
    default_source: str = "recent-unwrapped"
    command_timeout_seconds: int = 20


@dataclass(frozen=True)
class HcRuntimeConfig:
    socket_path: str | None = None
    allowed_terminal_addresses_file: str | None = None
    connect_timeout_seconds: float = 5
    request_timeout_seconds: float = 20
    event_idle_timeout_seconds: float = 35
    require_app_visible: bool = True
    include_hidden_workers: bool = False
    include_connected_machines: bool = False
    allowed_terminal_addresses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str = "herdr-tui"
    tui: HerdrCliConfig = field(default_factory=HerdrCliConfig)
    core: HcRuntimeConfig = field(default_factory=HcRuntimeConfig)


@dataclass(frozen=True)
class DiscordTransportConfig:
    enabled: bool = True


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    token: str = field(default="", repr=False)
    mode: str = "forum"
    allowed_chat_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    forum_chat_id: int | None = None
    statuses: tuple[str, ...] = ("blocked", "done")
    enable_send: bool = False
    enable_interrupt: bool = False
    confirmation_ttl_seconds: int = 60
    actor_mutations_per_minute: int = 6
    chat_mutations_per_minute: int = 12
    target_mutations_per_minute: int = 6
    poll_timeout_seconds: int = 25
    request_timeout_seconds: float = 35
    reconnect_delay_seconds: float = 5
    status_refresh_seconds: float = 5


@dataclass(frozen=True)
class ApprovalStrategy:
    method: str
    text: str | None = None
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatcherConfig:
    statuses: tuple[str, ...] = ("blocked", "done")
    reconnect_delay_seconds: float = 5
    resubscribe_interval_seconds: float = 300
    include_output: bool = True
    blocked_notification_delay_seconds: float = 1.5
    blocked_tail_lines: int = 80
    done_tail_lines: int = 60


@dataclass(frozen=True)
class AutoThreadsConfig:
    refresh_seconds: float = 30


@dataclass(frozen=True)
class StreamConfig:
    mode: str = "hooks"
    refresh_seconds: float = 8
    tail_lines: int = 1000
    initial_tail_lines: int = 60
    enable_visible_fallback: bool = True
    hook_inbox_path: str = "~/.cache/herdrelay/agent-output"
    hook_refresh_seconds: float = 1
    hook_max_event_bytes: int = 2_000_000
    hook_max_event_age_seconds: float = 86_400


@dataclass(frozen=True)
class AppConfig:
    discord_token: str
    herdr_socket_path: str | None = None
    allowed_guild_ids: frozenset[int] = frozenset()
    allowed_channel_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    thread_parent_channel_id: int | None = None
    database_path: str = "herdrelay.sqlite3"
    max_output_chars: int = 1800
    max_message_chars: int = 2000
    enable_send: bool = False
    enable_approve: bool = False
    enable_watcher: bool = False
    enable_stop: bool = False
    enable_auto_threads: bool = False
    enable_streaming: bool = False
    allow_pane_send_fallback: bool = False
    dangerous_text_blocklist: tuple[str, ...] = ()
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    auto_threads: AutoThreadsConfig = field(default_factory=AutoThreadsConfig)
    streaming: StreamConfig = field(default_factory=StreamConfig)
    discord: DiscordTransportConfig = field(default_factory=DiscordTransportConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    approval: dict[str, ApprovalStrategy] = field(default_factory=dict)
    deny: dict[str, ApprovalStrategy] = field(default_factory=dict)
    stop: ApprovalStrategy | None = None

    @property
    def herdr(self) -> HerdrCliConfig:
        """Compatibility alias for the legacy runtime configuration."""
        return self.runtime.tui


def load_config(
    path: str | Path = "config.yaml", *, require_discord_token: bool = True
) -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        data = loaded

    token = os.environ.get("DISCORD_TOKEN", "")

    runtime_data = data.get("runtime") or {}
    if not isinstance(runtime_data, dict):
        raise ValueError("runtime must be a YAML mapping")
    core_data = (
        runtime_data.get("herdr_core")
        if "herdr_core" in runtime_data
        else runtime_data.get("core")
    ) or {}
    if not isinstance(core_data, dict):
        raise ValueError("runtime.herdr_core must be a YAML mapping")
    tui_data = (
        runtime_data.get("herdr_tui")
        if "herdr_tui" in runtime_data
        else data.get("herdr")
    ) or {}
    if not isinstance(tui_data, dict):
        raise ValueError("runtime.herdr_tui must be a YAML mapping")
    transports_data = data.get("transports") or {}
    if not isinstance(transports_data, dict):
        raise ValueError("transports must be a YAML mapping")
    discord_data = transports_data.get("discord") or {}
    if not isinstance(discord_data, dict):
        raise ValueError("transports.discord must be a YAML mapping")
    telegram_data = transports_data.get("telegram") or {}
    if not isinstance(telegram_data, dict):
        raise ValueError("transports.telegram must be a YAML mapping")
    storage_data = data.get("storage") or {}
    if not isinstance(storage_data, dict):
        raise ValueError("storage must be a YAML mapping")
    relay_data = data.get("relay") or {}
    if not isinstance(relay_data, dict):
        raise ValueError("relay must be a YAML mapping")
    discord = DiscordTransportConfig(
        enabled=bool(discord_data.get("enabled", True))
    )
    if require_discord_token and discord.enabled and not token:
        raise ValueError("DISCORD_TOKEN is required when Discord is enabled")
    watcher_data = (
        discord_data.get("watcher")
        if "watcher" in discord_data
        else data.get("watcher")
    ) or {}
    auto_threads_data = (
        discord_data.get("auto_threads")
        if "auto_threads" in discord_data
        else data.get("auto_threads")
    ) or {}
    streaming_data = (
        relay_data.get("streaming")
        if "streaming" in relay_data
        else data.get("streaming")
    ) or {}
    approval_data = (
        discord_data.get("approval")
        if "approval" in discord_data
        else data.get("approval")
    ) or {}
    deny_data = (
        discord_data.get("deny")
        if "deny" in discord_data
        else data.get("deny")
    ) or {}
    stop_data = (
        discord_data.get("stop")
        if "stop" in discord_data
        else data.get("stop")
    )

    allowed_targets_file = _optional_config_path(
        core_data.get("allowed_terminal_addresses_file"),
        base=config_path.parent,
    )
    allowed_targets = _string_set(core_data.get("allowed_terminal_addresses"))
    if allowed_targets_file is not None:
        allowed_targets |= _read_target_allowlist(Path(allowed_targets_file))

    config = AppConfig(
        discord_token=token,
        herdr_socket_path=os.environ.get("HERDR_SOCKET_PATH") or None,
        allowed_guild_ids=_id_set(
            discord_data.get("allowed_guild_ids", data.get("allowed_guild_ids"))
        ),
        allowed_channel_ids=_id_set(
            discord_data.get("allowed_channel_ids", data.get("allowed_channel_ids"))
        ),
        allowed_user_ids=_id_set(
            discord_data.get("allowed_user_ids", data.get("allowed_user_ids"))
        ),
        thread_parent_channel_id=_optional_int(
            discord_data.get(
                "thread_parent_channel_id", data.get("thread_parent_channel_id")
            )
        ),
        database_path=_database_path(
            storage_data.get("database_path") or data.get("database_path")
        ),
        max_output_chars=int(
            relay_data.get("max_output_chars", data.get("max_output_chars", 1800))
        ),
        max_message_chars=int(
            relay_data.get("max_message_chars", data.get("max_message_chars", 2000))
        ),
        enable_send=bool(
            discord_data.get("enable_send", data.get("enable_send", False))
        ),
        enable_approve=bool(
            discord_data.get("enable_approve", data.get("enable_approve", False))
        ),
        enable_watcher=bool(
            discord_data.get("enable_watcher", data.get("enable_watcher", False))
        ),
        enable_stop=bool(
            discord_data.get("enable_stop", data.get("enable_stop", False))
        ),
        enable_auto_threads=bool(
            discord_data.get(
                "enable_auto_threads", data.get("enable_auto_threads", False)
            )
        ),
        enable_streaming=bool(
            relay_data.get("enable_streaming", data.get("enable_streaming", False))
        ),
        allow_pane_send_fallback=bool(
            discord_data.get(
                "allow_pane_send_fallback",
                data.get("allow_pane_send_fallback", False),
            )
        ),
        dangerous_text_blocklist=tuple(
            str(item)
            for item in relay_data.get(
                "dangerous_text_blocklist",
                data.get("dangerous_text_blocklist") or (),
            )
        ),
        runtime=RuntimeConfig(
            provider=_runtime_provider(runtime_data.get("provider")),
            tui=HerdrCliConfig(
                cli_path=str(tui_data.get("cli_path") or "herdr"),
                default_source=str(
                    tui_data.get("default_source") or "recent-unwrapped"
                ),
                command_timeout_seconds=int(
                    tui_data.get("command_timeout_seconds", 20)
                ),
            ),
            core=HcRuntimeConfig(
                socket_path=(
                    str(core_data["socket_path"])
                    if core_data.get("socket_path")
                    else None
                ),
                allowed_terminal_addresses_file=allowed_targets_file,
                connect_timeout_seconds=float(
                    core_data.get("connect_timeout_seconds", 5)
                ),
                request_timeout_seconds=float(
                    core_data.get("request_timeout_seconds", 20)
                ),
                event_idle_timeout_seconds=float(
                    core_data.get("event_idle_timeout_seconds", 35)
                ),
                require_app_visible=bool(
                    core_data.get("require_app_visible", True)
                ),
                include_hidden_workers=bool(
                    core_data.get("include_hidden_workers", False)
                ),
                include_connected_machines=bool(
                    core_data.get("include_connected_machines", False)
                ),
                allowed_terminal_addresses=allowed_targets,
            ),
        ),
        watcher=WatcherConfig(
            statuses=tuple(
                str(item)
                for item in watcher_data.get("statuses", ("blocked", "done"))
            ),
            reconnect_delay_seconds=float(watcher_data.get("reconnect_delay_seconds", 5)),
            resubscribe_interval_seconds=float(
                watcher_data.get("resubscribe_interval_seconds", 300)
            ),
            include_output=bool(watcher_data.get("include_output", True)),
            blocked_notification_delay_seconds=float(
                watcher_data.get("blocked_notification_delay_seconds", 1.5)
            ),
            blocked_tail_lines=int(watcher_data.get("blocked_tail_lines", 80)),
            done_tail_lines=int(watcher_data.get("done_tail_lines", 60)),
        ),
        auto_threads=AutoThreadsConfig(
            refresh_seconds=float(auto_threads_data.get("refresh_seconds", 30)),
        ),
        streaming=StreamConfig(
            mode=_stream_mode(streaming_data.get("mode")),
            refresh_seconds=float(streaming_data.get("refresh_seconds", 8)),
            tail_lines=int(streaming_data.get("tail_lines", 1000)),
            initial_tail_lines=int(streaming_data.get("initial_tail_lines", 60)),
            enable_visible_fallback=bool(
                streaming_data.get("enable_visible_fallback", True)
            ),
            hook_inbox_path=str(
                streaming_data.get("hook_inbox_path")
                or "~/.cache/herdrelay/agent-output"
            ),
            hook_refresh_seconds=float(
                streaming_data.get("hook_refresh_seconds", 1)
            ),
            hook_max_event_bytes=int(
                streaming_data.get("hook_max_event_bytes", 2_000_000)
            ),
            hook_max_event_age_seconds=float(
                streaming_data.get("hook_max_event_age_seconds", 86_400)
            ),
        ),
        discord=discord,
        telegram=TelegramConfig(
            enabled=bool(telegram_data.get("enabled", False)),
            token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            mode=str(telegram_data.get("mode") or "forum").casefold(),
            allowed_chat_ids=_id_set(telegram_data.get("allowed_chat_ids")),
            allowed_user_ids=_id_set(telegram_data.get("allowed_user_ids")),
            forum_chat_id=_optional_int(telegram_data.get("forum_chat_id")),
            statuses=tuple(
                str(item).casefold()
                for item in telegram_data.get("statuses", ("blocked", "done"))
            ),
            enable_send=bool(telegram_data.get("enable_send", False)),
            enable_interrupt=bool(telegram_data.get("enable_interrupt", False)),
            confirmation_ttl_seconds=int(
                telegram_data.get("confirmation_ttl_seconds", 60)
            ),
            actor_mutations_per_minute=int(
                telegram_data.get("actor_mutations_per_minute", 6)
            ),
            chat_mutations_per_minute=int(
                telegram_data.get("chat_mutations_per_minute", 12)
            ),
            target_mutations_per_minute=int(
                telegram_data.get("target_mutations_per_minute", 6)
            ),
            poll_timeout_seconds=int(
                telegram_data.get("poll_timeout_seconds", 25)
            ),
            request_timeout_seconds=float(
                telegram_data.get("request_timeout_seconds", 35)
            ),
            reconnect_delay_seconds=float(
                telegram_data.get("reconnect_delay_seconds", 5)
            ),
            status_refresh_seconds=float(
                telegram_data.get("status_refresh_seconds", 5)
            ),
        ),
        approval=_approval_strategies(approval_data),
        deny=_approval_strategies(deny_data),
        stop=_single_strategy(stop_data),
    )
    _validate_runtime_features(config)
    _validate_telegram(config)
    return config


def _validate_telegram(config: AppConfig) -> None:
    telegram = config.telegram
    if not telegram.enabled:
        return
    if not telegram.token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required when Telegram is enabled")
    if telegram.mode != "forum":
        raise ValueError("transports.telegram.mode currently supports only 'forum'")
    if not telegram.allowed_chat_ids:
        raise ValueError("Telegram requires at least one allowed_chat_id")
    if not telegram.allowed_user_ids:
        raise ValueError("Telegram requires at least one allowed_user_id")
    if telegram.forum_chat_id is None:
        raise ValueError("Telegram requires forum_chat_id for status delivery")
    if telegram.forum_chat_id not in telegram.allowed_chat_ids:
        raise ValueError("Telegram forum_chat_id must be in allowed_chat_ids")
    if not telegram.statuses or any(
        status not in {"blocked", "done"} for status in telegram.statuses
    ):
        raise ValueError("Telegram statuses may contain only blocked and done")
    if not 10 <= telegram.confirmation_ttl_seconds <= 600:
        raise ValueError(
            "Telegram confirmation_ttl_seconds must be between 10 and 600"
        )
    for name, value in (
        ("actor_mutations_per_minute", telegram.actor_mutations_per_minute),
        ("chat_mutations_per_minute", telegram.chat_mutations_per_minute),
        ("target_mutations_per_minute", telegram.target_mutations_per_minute),
    ):
        if not 1 <= value <= 1000:
            raise ValueError(f"Telegram {name} must be between 1 and 1000")
    if not 1 <= telegram.poll_timeout_seconds <= 50:
        raise ValueError("Telegram poll_timeout_seconds must be between 1 and 50")
    if telegram.request_timeout_seconds <= telegram.poll_timeout_seconds:
        raise ValueError(
            "Telegram request_timeout_seconds must exceed poll_timeout_seconds"
        )
    if telegram.reconnect_delay_seconds <= 0:
        raise ValueError("Telegram reconnect_delay_seconds must be positive")
    if telegram.status_refresh_seconds <= 0:
        raise ValueError("Telegram status_refresh_seconds must be positive")


def _validate_runtime_features(config: AppConfig) -> None:
    if config.runtime.provider != "herdr-core":
        return
    if config.runtime.core.connect_timeout_seconds <= 0:
        raise ValueError("runtime.herdr_core.connect_timeout_seconds must be positive")
    if config.runtime.core.request_timeout_seconds <= 0:
        raise ValueError("runtime.herdr_core.request_timeout_seconds must be positive")
    if config.runtime.core.event_idle_timeout_seconds <= 15:
        raise ValueError(
            "runtime.herdr_core.event_idle_timeout_seconds must exceed Core's 15s heartbeat"
        )
    if config.enable_approve:
        raise ValueError(
            "enable_approve is disabled for Herdr Core until guarded actions exist"
        )


def _runtime_provider(value: Any) -> str:
    provider = str(value or "herdr-tui").casefold()
    aliases = {"tui": "herdr-tui", "core": "herdr-core", "hc": "herdr-core"}
    provider = aliases.get(provider, provider)
    if provider not in {"herdr-tui", "herdr-core"}:
        raise ValueError("runtime.provider must be 'herdr-tui' or 'herdr-core'")
    return provider


def _stream_mode(value: Any) -> str:
    mode = str(value or "hooks").casefold()
    if mode not in {"hooks", "poll"}:
        raise ValueError("streaming.mode must be 'hooks' or 'poll'")
    return mode


def _string_set(value: Any) -> frozenset[str]:
    if not value:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError("string allowlists must be YAML lists")
    result = frozenset(str(item).strip() for item in value)
    if not all(result):
        raise ValueError("string allowlists cannot contain empty values")
    return result


def _database_path(value: Any) -> str:
    if value not in (None, ""):
        return str(value)
    preferred = Path("herdrelay.sqlite3")
    legacy = Path("herdr-discord-bridge.sqlite3")
    if legacy.exists() and not preferred.exists():
        return str(legacy)
    return str(preferred)


def _optional_config_path(value: Any, *, base: Path) -> str | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _read_target_allowlist(path: Path) -> frozenset[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return frozenset()
    targets = {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }
    if any(any(character.isspace() for character in target) for target in targets):
        raise ValueError(f"Core target allowlist contains whitespace: {path}")
    return frozenset(targets)


def _id_set(value: Any) -> frozenset[int]:
    if not value:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError("ID allowlists must be YAML lists")
    return frozenset(int(item) for item in value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _single_strategy(value: Any) -> ApprovalStrategy | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("stop must be a YAML mapping")
    return ApprovalStrategy(
        method=str(value.get("method") or ""),
        text=value.get("text"),
        keys=tuple(str(key) for key in value.get("keys") or ()),
    )


def _approval_strategies(value: Any) -> dict[str, ApprovalStrategy]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("approval must be a YAML mapping")
    strategies: dict[str, ApprovalStrategy] = {}
    for agent, raw in value.items():
        if not isinstance(raw, dict):
            raise ValueError(f"approval.{agent} must be a mapping")
        strategies[str(agent)] = ApprovalStrategy(
            method=str(raw.get("method") or ""),
            text=raw.get("text"),
            keys=tuple(str(key) for key in raw.get("keys") or ()),
        )
    return strategies
