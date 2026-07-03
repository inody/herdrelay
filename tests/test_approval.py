import pytest

from herdr_discord_bridge.approval import ApprovalError, ensure_blocked_for_approval, strategy_for
from herdr_discord_bridge.config import AppConfig, ApprovalStrategy
from herdr_discord_bridge.models import HerdrTarget


def test_strategy_for_agent_specific_config():
    config = AppConfig(
        discord_token="token",
        approval={"codex": ApprovalStrategy(method="send_text_enter", text="y")},
    )

    strategy = strategy_for(config, "codex")

    assert strategy.method == "send_text_enter"
    assert strategy.text == "y"


def test_strategy_for_default_config():
    config = AppConfig(
        discord_token="token",
        approval={"default": ApprovalStrategy(method="send_keys", keys=("Enter",))},
    )

    strategy = strategy_for(config, "unknown-agent")

    assert strategy.method == "send_keys"
    assert strategy.keys == ("Enter",)


def test_approval_requires_blocked_status():
    ensure_blocked_for_approval(HerdrTarget(target="w1:p1", status="blocked"))
    ensure_blocked_for_approval(HerdrTarget(target="w1:p1", status="BLOCKED"))

    with pytest.raises(ApprovalError, match="current status is idle"):
        ensure_blocked_for_approval(HerdrTarget(target="w1:p1", status="idle"))

    with pytest.raises(ApprovalError, match="current status is unknown"):
        ensure_blocked_for_approval(HerdrTarget(target="w1:p1"))

