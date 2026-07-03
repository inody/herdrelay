from herdr_discord_bridge.herdr_cli import _read_text


def test_read_text_extracts_agent_read_payload():
    output = _read_text(
        '{"id":"cli:agent:read","result":{"read":{"format":"text","pane_id":"w7:p1","text":"hello"}}}'
    )

    assert output == "hello"


def test_read_text_keeps_plain_text():
    assert _read_text("plain output") == "plain output"

