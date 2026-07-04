from herdr_discord_bridge.formatter import (
    format_tail,
    target_alias,
)
from herdr_discord_bridge.models import HerdrTarget


def test_tail_escapes_code_fences():
    output = format_tail("before\n```\nafter")

    assert "`\u200b``" in output


def test_tail_truncation_keeps_recent_output():
    output = format_tail("old context\n" + ("x" * 200) + "\nAPPROVE THIS?", max_chars=80)

    assert "old context" not in output
    assert "APPROVE THIS?" in output
    assert output.startswith("```text\n... truncated")
    assert output.endswith("\n```")


def test_target_alias_prefers_workspace_label():
    target = HerdrTarget(
        target="w7:p3",
        workspace_label="herdr-chat-bridge",
        agent_name="claude",
        cwd="/x/herdr-chat-bridge",
    )
    assert target_alias(target) == "herdr-chat-bridge"


def test_target_alias_falls_back_to_cwd_basename():
    target = HerdrTarget(
        target="w7:p3",
        workspace_label="~",
        cwd="/Users/dinoue/Dropbox/univ/cbo_mppi",
    )
    assert target_alias(target) == "cbo_mppi"


def test_target_alias_falls_back_to_target_when_unknown():
    target = HerdrTarget(target="w7:p3")
    assert target_alias(target) == "w7:p3"
