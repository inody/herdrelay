from __future__ import annotations

from .config import AppConfig, ApprovalStrategy
from .herdr_client import HerdrClient


class ApprovalError(ValueError):
    pass


def strategy_for(config: AppConfig, agent_name: str | None) -> ApprovalStrategy:
    if agent_name and agent_name in config.approval:
        return config.approval[agent_name]
    if "default" in config.approval:
        return config.approval["default"]
    raise ApprovalError("No approval strategy is configured for this target.")


def apply_approval(client: HerdrClient, target: str, strategy: ApprovalStrategy) -> None:
    if strategy.method == "send_keys":
        if not strategy.keys:
            raise ApprovalError("send_keys approval requires at least one key.")
        client.send_keys(target, strategy.keys)
    elif strategy.method == "send_text_enter":
        if strategy.text is None:
            raise ApprovalError("send_text_enter approval requires text.")
        client.send_text_enter(target, strategy.text)
    else:
        raise ApprovalError(f"Unsupported approval method: {strategy.method}")

