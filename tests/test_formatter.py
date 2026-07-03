from herdr_discord_bridge.formatter import format_bindings, format_status, format_tail
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

