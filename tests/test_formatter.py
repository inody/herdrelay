from herdr_discord_bridge.formatter import (
    format_bindings,
    format_status,
    format_target_card,
    format_tail,
    status_emoji,
    target_alias,
)
from herdr_discord_bridge.models import Binding, HerdrTarget


def test_status_formats_targets():
    output = format_status(
        [
            HerdrTarget(
                target="1-1",
                kind="agent",
                label="bridge",
                agent_name="codex",
                status="working",
                cwd="/tmp/project",
            )
        ]
    )

    assert output.startswith("```text")
    assert "working" in output
    assert "codex" in output
    assert "1-1" in output


def test_tail_escapes_code_fences():
    output = format_tail("before\n```\nafter")

    assert "`\u200b``" in output


def test_tail_truncation_keeps_recent_output():
    output = format_tail("old context\n" + ("x" * 200) + "\nAPPROVE THIS?", max_chars=80)

    assert "old context" not in output
    assert "APPROVE THIS?" in output
    assert output.startswith("```text\n... truncated")
    assert output.endswith("\n```")


def test_bindings_empty_message():
    assert format_bindings([]) == "No bindings."


def test_bindings_formats_rows():
    output = format_bindings(
        [
            Binding(
                guild_id="1",
                channel_id="2",
                thread_id="3",
                herdr_target="1-1",
                label="main",
                created_by="10",
                created_at="now",
                updated_at="now",
            )
        ]
    )

    assert "3 -> 1-1 (main)" in output


def test_status_emoji_maps_known_statuses():
    assert status_emoji("blocked") == "🔴"
    assert status_emoji("WORKING") == "🟡"
    assert status_emoji(None) == "⚪"
    assert status_emoji("weird") == "⚪"


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


def test_format_target_card_with_tail_preview():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="cbo_mppi",
        agent_name="codex",
        status="blocked",
        cwd="/x/cbo_mppi",
    )
    card = format_target_card(target, tail_preview="Allow Edit to foo.py?")

    assert "🔴 cbo_mppi/codex" in card
    assert "`w1:p1`" in card
    assert "`/x/cbo_mppi`" in card
    assert "`blocked`" in card
    assert "Allow Edit to foo.py?" in card


def test_format_target_card_without_tail_preview_omits_block():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="cbo_mppi",
        agent_name="codex",
        status="idle",
    )
    card = format_target_card(target)

    assert "🟢 cbo_mppi/codex" in card
    assert "(no output)" not in card
