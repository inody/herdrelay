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
class HcTargetExposureConfig:
    mode: str = "all-eligible"
    addresses: frozenset[str] = frozenset()
    addresses_file: str | None = None


@dataclass(frozen=True)
class HcRuntimeConfig:
    socket_path: str | None = None
    connect_timeout_seconds: float = 5
    request_timeout_seconds: float = 20
    event_idle_timeout_seconds: float = 35
    require_app_visible: bool = True
    include_hidden_workers: bool = False
    include_connected_machines: bool = False
    target_exposure: HcTargetExposureConfig = field(
        default_factory=HcTargetExposureConfig
    )


@dataclass(frozen=True)
class OrcaTargetExposureConfig:
    mode: str = "all-eligible"
    pane_keys: frozenset[str] = frozenset()
    worktree_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OrcaRuntimeConfig:
    cli_path: str = "orca"
    environment: str = "local"
    command_timeout_seconds: float = 10
    refresh_seconds: float = 2
    send_wait_seconds: float = 5
    include_terminal_agents: bool = True
    include_chats: bool = False
    host_ids: frozenset[str] = frozenset({"local"})
    target_exposure: OrcaTargetExposureConfig = field(
        default_factory=OrcaTargetExposureConfig
    )


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str = "herdr-tui"
    tui: HerdrCliConfig = field(default_factory=HerdrCliConfig)
    core: HcRuntimeConfig = field(default_factory=HcRuntimeConfig)
    orca: OrcaRuntimeConfig = field(default_factory=OrcaRuntimeConfig)


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
    enable_questions: bool = False
    enable_watcher: bool = False
    enable_interrupt: bool = False
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
    orca_data = runtime_data.get("orca") or {}
    if not isinstance(orca_data, dict):
        raise ValueError("runtime.orca must be a YAML mapping")
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
    core_target_exposure = _core_target_exposure(
        core_data, base=config_path.parent
    )
    orca_target_exposure = _orca_target_exposure(orca_data)
    orca_host_ids = _orca_host_ids(orca_data)
    runtime_provider = _runtime_provider(runtime_data.get("provider"))

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
        enable_questions=bool(
            discord_data.get(
                "enable_questions",
                discord_data.get(
                    "enable_approve",
                    data.get("enable_questions", data.get("enable_approve", False)),
                ),
            )
        ),
        enable_watcher=bool(
            discord_data.get("enable_watcher", data.get("enable_watcher", False))
        ),
        enable_interrupt=bool(
            discord_data.get(
                "enable_interrupt",
                data.get(
                    "enable_interrupt",
                    (
                        discord_data.get("enable_stop", data.get("enable_stop", False))
                        if runtime_provider == "herdr-core"
                        else False
                    ),
                ),
            )
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
            provider=runtime_provider,
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
                target_exposure=core_target_exposure,
            ),
            orca=OrcaRuntimeConfig(
                cli_path=str(orca_data.get("cli_path") or "orca"),
                environment=str(orca_data.get("environment") or "local"),
                command_timeout_seconds=float(
                    orca_data.get("command_timeout_seconds", 10)
                ),
                refresh_seconds=float(orca_data.get("refresh_seconds", 2)),
                send_wait_seconds=float(orca_data.get("send_wait_seconds", 5)),
                include_terminal_agents=bool(
                    orca_data.get("include_terminal_agents", True)
                ),
                include_chats=bool(orca_data.get("include_chats", False)),
                host_ids=orca_host_ids,
                target_exposure=orca_target_exposure,
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
    if config.runtime.provider == "herdr-core":
        core = config.runtime.core
        _validate_target_exposure(
            mode=core.target_exposure.mode,
            selectors=core.target_exposure.addresses,
            path="runtime.herdr_core.target_exposure",
        )
        if core.connect_timeout_seconds <= 0:
            raise ValueError("runtime.herdr_core.connect_timeout_seconds must be positive")
        if config.runtime.core.request_timeout_seconds <= 0:
            raise ValueError("runtime.herdr_core.request_timeout_seconds must be positive")
        if config.runtime.core.event_idle_timeout_seconds <= 15:
            raise ValueError(
                "runtime.herdr_core.event_idle_timeout_seconds must exceed Core's 15s heartbeat"
            )
        if config.enable_questions:
            raise ValueError(
                "Discord question answering is unavailable for Herdr Core"
            )
        return
    if config.runtime.provider == "herdr-tui":
        if config.enable_interrupt:
            raise ValueError(
                "Discord interrupt requires a guarded native backend operation"
            )
        return
    if config.runtime.provider != "orca":
        return

    orca = config.runtime.orca
    if orca.command_timeout_seconds <= 0:
        raise ValueError("runtime.orca.command_timeout_seconds must be positive")
    if orca.refresh_seconds <= 0:
        raise ValueError("runtime.orca.refresh_seconds must be positive")
    if orca.send_wait_seconds < 0:
        raise ValueError("runtime.orca.send_wait_seconds must not be negative")
    if not orca.environment.strip():
        raise ValueError("runtime.orca.environment must not be empty")
    if not orca.host_ids:
        raise ValueError("runtime.orca.host_ids must not be empty")
    _validate_target_exposure(
        mode=orca.target_exposure.mode,
        selectors=(
            orca.target_exposure.pane_keys
            | orca.target_exposure.worktree_ids
        ),
        path="runtime.orca.target_exposure",
    )
    if config.enable_questions or config.enable_interrupt:
        raise ValueError(
            "Discord question answering and interrupt are unavailable for Orca"
        )
    if config.enable_watcher:
        raise ValueError("Discord watcher is not implemented for the Orca backend")
    if config.enable_streaming and config.streaming.mode != "hooks":
        raise ValueError("Orca response streaming requires relay.streaming.mode: hooks")
    if config.telegram.enable_interrupt:
        raise ValueError(
            "Telegram interrupt is not safely supported by the Orca backend"
        )


def _validate_target_exposure(
    *, mode: str, selectors: frozenset[str], path: str
) -> None:
    if mode == "selected" and not selectors:
        raise ValueError(f"{path}.mode 'selected' requires exact selectors")
    if mode in {"all-eligible", "none"} and selectors:
        raise ValueError(f"{path}.mode '{mode}' cannot include selectors")


def _core_target_exposure(
    data: dict[str, Any], *, base: Path
) -> HcTargetExposureConfig:
    legacy_keys = {
        "allowed_terminal_addresses",
        "allowed_terminal_addresses_file",
    }
    legacy_present = any(key in data for key in legacy_keys)
    raw = data.get("target_exposure")
    if raw is not None and legacy_present:
        raise ValueError(
            "runtime.herdr_core.target_exposure conflicts with legacy allowed_* keys"
        )
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError(
                "runtime.herdr_core.target_exposure must be a YAML mapping"
            )
        mode = _target_exposure_mode(raw.get("mode"))
        addresses_file = _optional_config_path(
            raw.get("addresses_file"), base=base
        )
        addresses = _string_set(raw.get("addresses"))
    else:
        addresses_file = _optional_config_path(
            data.get("allowed_terminal_addresses_file"), base=base
        )
        addresses = _string_set(data.get("allowed_terminal_addresses"))
        mode = "all-eligible"
    if addresses_file is not None:
        addresses |= _read_target_allowlist(Path(addresses_file))
    if raw is not None and mode != "selected" and addresses_file is not None:
        raise ValueError(
            "runtime.herdr_core.target_exposure addresses_file requires selected mode"
        )
    if raw is None and legacy_present:
        mode = "selected" if addresses else "none"
    return HcTargetExposureConfig(
        mode=mode,
        addresses=addresses,
        addresses_file=addresses_file,
    )


def _orca_target_exposure(data: dict[str, Any]) -> OrcaTargetExposureConfig:
    legacy_keys = {"allowed_pane_keys", "allowed_worktree_ids"}
    legacy_present = any(key in data for key in legacy_keys)
    raw = data.get("target_exposure")
    if raw is not None and legacy_present:
        raise ValueError(
            "runtime.orca.target_exposure conflicts with legacy allowed_* keys"
        )
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("runtime.orca.target_exposure must be a YAML mapping")
        mode = _target_exposure_mode(raw.get("mode"))
        pane_keys = _string_set(raw.get("pane_keys"))
        worktree_ids = _string_set(raw.get("worktree_ids"))
    else:
        pane_keys = _string_set(data.get("allowed_pane_keys"))
        worktree_ids = _string_set(data.get("allowed_worktree_ids"))
        mode = "all-eligible"
    if raw is None and legacy_present:
        mode = "selected" if pane_keys or worktree_ids else "none"
    return OrcaTargetExposureConfig(
        mode=mode,
        pane_keys=pane_keys,
        worktree_ids=worktree_ids,
    )


def _orca_host_ids(data: dict[str, Any]) -> frozenset[str]:
    if "host_ids" in data and "allowed_host_ids" in data:
        raise ValueError(
            "runtime.orca.host_ids conflicts with legacy allowed_host_ids"
        )
    if "host_ids" in data:
        return _string_set(data.get("host_ids"))
    if "allowed_host_ids" in data:
        return _string_set(data.get("allowed_host_ids"))
    return frozenset({"local"})


def _target_exposure_mode(value: Any) -> str:
    mode = str(value or "all-eligible").casefold()
    if mode not in {"all-eligible", "selected", "none"}:
        raise ValueError(
            "target exposure mode must be 'all-eligible', 'selected', or 'none'"
        )
    return mode


def _runtime_provider(value: Any) -> str:
    provider = str(value or "herdr-tui").casefold()
    aliases = {"tui": "herdr-tui", "core": "herdr-core", "hc": "herdr-core"}
    provider = aliases.get(provider, provider)
    if provider not in {"herdr-tui", "herdr-core", "orca"}:
        raise ValueError(
            "runtime.provider must be 'herdr-tui', 'herdr-core', or 'orca'"
        )
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
        raise ValueError(f"Core target selection contains whitespace: {path}")
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
