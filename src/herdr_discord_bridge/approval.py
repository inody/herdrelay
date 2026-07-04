from __future__ import annotations

from .config import AppConfig, ApprovalStrategy
from .herdr_client import HerdrClient
from .models import HerdrTarget


class ApprovalError(ValueError):
    pass


def strategy_for(config: AppConfig, agent_name: str | None) -> ApprovalStrategy:
    if agent_name and agent_name in config.approval:
        return config.approval[agent_name]
    if "default" in config.approval:
        return config.approval["default"]
    raise ApprovalError("No approval strategy is configured for this target.")


def deny_strategy_for(config: AppConfig, agent_name: str | None) -> ApprovalStrategy:
    if agent_name and agent_name in config.deny:
        return config.deny[agent_name]
    if "default" in config.deny:
        return config.deny["default"]
    raise ApprovalError("No deny strategy is configured for this target.")


def stop_strategy_for(config: AppConfig) -> ApprovalStrategy:
    if config.stop is None:
        raise ApprovalError("No stop strategy is configured.")
    return config.stop


def ensure_blocked_for_approval(target: HerdrTarget) -> None:
    status = target.status or "unknown"
    if status.casefold() != "blocked":
        raise ApprovalError(f"Approval requires blocked status; current status is {status}.")


def apply_action(client: HerdrClient, target: str, strategy: ApprovalStrategy) -> None:
    if strategy.method == "send_keys":
        if not strategy.keys:
            raise ApprovalError("send_keys requires at least one key.")
        client.send_keys(target, strategy.keys)
    elif strategy.method == "send_text_enter":
        if strategy.text is None:
            raise ApprovalError("send_text_enter requires text.")
        client.send_text_enter(target, strategy.text)
    else:
        raise ApprovalError(f"Unsupported method: {strategy.method}")


# Backwards-compatible alias for callers expecting the approval-specific name.
def apply_approval(client: HerdrClient, target: str, strategy: ApprovalStrategy) -> None:
    apply_action(client, target, strategy)
