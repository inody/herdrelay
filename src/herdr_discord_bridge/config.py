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
class ApprovalStrategy:
    method: str
    text: str | None = None
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatcherConfig:
    statuses: tuple[str, ...] = ("blocked", "done")
    reconnect_delay_seconds: float = 5
    resubscribe_interval_seconds: float = 300
    blocked_tail_lines: int = 80
    done_tail_lines: int = 60


@dataclass(frozen=True)
class AutoThreadsConfig:
    refresh_seconds: float = 30


@dataclass(frozen=True)
class StreamConfig:
    refresh_seconds: float = 8
    tail_lines: int = 60


@dataclass(frozen=True)
class AppConfig:
    discord_token: str
    herdr_socket_path: str | None = None
    allowed_guild_ids: frozenset[int] = frozenset()
    allowed_channel_ids: frozenset[int] = frozenset()
    allowed_user_ids: frozenset[int] = frozenset()
    thread_parent_channel_id: int | None = None
    database_path: str = "herdr-discord-bridge.sqlite3"
    max_output_chars: int = 1800
    max_message_chars: int = 2000
    enable_send: bool = False
    enable_approve: bool = False
    enable_watcher: bool = False
    enable_stop: bool = False
    enable_auto_threads: bool = False
    enable_streaming: bool = False
    allow_pane_send_fallback: bool = False
    submit_after_agent_send: bool = True
    submit_after_agent_send_delay_seconds: float = 0.5
    dangerous_text_blocklist: tuple[str, ...] = ()
    herdr: HerdrCliConfig = field(default_factory=HerdrCliConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    auto_threads: AutoThreadsConfig = field(default_factory=AutoThreadsConfig)
    streaming: StreamConfig = field(default_factory=StreamConfig)
    approval: dict[str, ApprovalStrategy] = field(default_factory=dict)
    deny: dict[str, ApprovalStrategy] = field(default_factory=dict)
    stop: ApprovalStrategy | None = None


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    load_dotenv()
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")
        data = loaded

    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise ValueError("DISCORD_TOKEN is required in .env or environment")

    herdr_data = data.get("herdr") or {}
    watcher_data = data.get("watcher") or {}
    auto_threads_data = data.get("auto_threads") or {}
    streaming_data = data.get("streaming") or {}
    approval_data = data.get("approval") or {}

    return AppConfig(
        discord_token=token,
        herdr_socket_path=os.environ.get("HERDR_SOCKET_PATH") or None,
        allowed_guild_ids=_id_set(data.get("allowed_guild_ids")),
        allowed_channel_ids=_id_set(data.get("allowed_channel_ids")),
        allowed_user_ids=_id_set(data.get("allowed_user_ids")),
        thread_parent_channel_id=_optional_int(data.get("thread_parent_channel_id")),
        database_path=str(data.get("database_path") or "herdr-discord-bridge.sqlite3"),
        max_output_chars=int(data.get("max_output_chars", 1800)),
        max_message_chars=int(data.get("max_message_chars", 2000)),
        enable_send=bool(data.get("enable_send", False)),
        enable_approve=bool(data.get("enable_approve", False)),
        enable_watcher=bool(data.get("enable_watcher", False)),
        enable_stop=bool(data.get("enable_stop", False)),
        enable_auto_threads=bool(data.get("enable_auto_threads", False)),
        enable_streaming=bool(data.get("enable_streaming", False)),
        allow_pane_send_fallback=bool(data.get("allow_pane_send_fallback", False)),
        submit_after_agent_send=bool(data.get("submit_after_agent_send", True)),
        submit_after_agent_send_delay_seconds=float(
            data.get("submit_after_agent_send_delay_seconds", 0.5)
        ),
        dangerous_text_blocklist=tuple(str(item) for item in data.get("dangerous_text_blocklist") or ()),
        herdr=HerdrCliConfig(
            cli_path=str(herdr_data.get("cli_path") or "herdr"),
            default_source=str(herdr_data.get("default_source") or "recent-unwrapped"),
            command_timeout_seconds=int(herdr_data.get("command_timeout_seconds", 20)),
        ),
        watcher=WatcherConfig(
            statuses=tuple(str(item) for item in watcher_data.get("statuses") or ("blocked", "done")),
            reconnect_delay_seconds=float(watcher_data.get("reconnect_delay_seconds", 5)),
            resubscribe_interval_seconds=float(
                watcher_data.get("resubscribe_interval_seconds", 300)
            ),
            blocked_tail_lines=int(watcher_data.get("blocked_tail_lines", 80)),
            done_tail_lines=int(watcher_data.get("done_tail_lines", 60)),
        ),
        auto_threads=AutoThreadsConfig(
            refresh_seconds=float(auto_threads_data.get("refresh_seconds", 30)),
        ),
        streaming=StreamConfig(
            refresh_seconds=float(streaming_data.get("refresh_seconds", 8)),
            tail_lines=int(streaming_data.get("tail_lines", 60)),
        ),
        approval=_approval_strategies(approval_data),
        deny=_approval_strategies(data.get("deny") or {}),
        stop=_single_strategy(data.get("stop")),
    )


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
