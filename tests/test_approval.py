import pytest

from herdr_discord_bridge.approval import (
    ApprovalError,
    apply_action,
    deny_strategy_for,
    ensure_blocked_for_approval,
    stop_strategy_for,
    strategy_for,
)
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


def test_deny_strategy_picks_agent_specific():
    config = AppConfig(
        discord_token="token",
        deny={"claude": ApprovalStrategy(method="send_keys", keys=("Escape",))},
    )

    strategy = deny_strategy_for(config, "claude")

    assert strategy.method == "send_keys"
    assert strategy.keys == ("Escape",)


def test_deny_strategy_missing_raises():
    config = AppConfig(discord_token="token")

    with pytest.raises(ApprovalError, match="No deny strategy"):
        deny_strategy_for(config, "claude")


def test_stop_strategy_returns_configured():
    config = AppConfig(
        discord_token="token",
        stop=ApprovalStrategy(method="send_keys", keys=("C-c",)),
    )

    assert stop_strategy_for(config).keys == ("C-c",)


def test_stop_strategy_missing_raises():
    config = AppConfig(discord_token="token")

    with pytest.raises(ApprovalError, match="No stop strategy"):
        stop_strategy_for(config)


def test_apply_action_routes_by_method():
    calls = []

    class FakeClient:
        def send_keys(self, target, keys):
            calls.append(("send_keys", target, tuple(keys)))

        def send_text_enter(self, target, text):
            calls.append(("send_text_enter", target, text))

    client = FakeClient()

    apply_action(client, "w1:p1", ApprovalStrategy(method="send_keys", keys=("Enter",)))
    apply_action(client, "w1:p1", ApprovalStrategy(method="send_text_enter", text="y"))

    assert calls == [
        ("send_keys", "w1:p1", ("Enter",)),
        ("send_text_enter", "w1:p1", "y"),
    ]


def test_apply_action_rejects_empty_keys():
    class FakeClient:
        def send_keys(self, target, keys):
            raise AssertionError("should not be called")

        def send_text_enter(self, target, text):
            raise AssertionError("should not be called")

    with pytest.raises(ApprovalError, match="send_keys requires"):
        apply_action(
            FakeClient(), "w1:p1", ApprovalStrategy(method="send_keys", keys=())
        )

