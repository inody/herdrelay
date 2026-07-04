from herdr_discord_bridge.models import HerdrTarget
from herdr_discord_bridge.threads import thread_name


def test_thread_name_marks_blocked_panes():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="cbo_mppi",
        agent_name="codex",
        status="blocked",
    )

    assert thread_name(target) == "🔴 cbo_mppi/codex"


def test_thread_name_has_no_marker_when_not_blocked():
    blocked = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="blocked"
    )
    working = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="working"
    )
    idle = HerdrTarget(
        target="w7:p5", workspace_label="herdr-chat-bridge", agent_name="claude", status="idle"
    )

    assert thread_name(blocked).startswith("🔴")
    assert not thread_name(working).startswith("🔴")
    assert not thread_name(idle).startswith("🔴")
    assert thread_name(working) == "herdr-chat-bridge/claude"


def test_thread_name_truncates_to_discord_limit():
    target = HerdrTarget(
        target="w1:p1",
        workspace_label="a" * 200,
        agent_name="codex",
        status="idle",
    )

    assert len(thread_name(target)) <= 100
