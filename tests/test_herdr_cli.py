from herdr_discord_bridge.config import HerdrCliConfig
from herdr_discord_bridge.herdr_cli import CommandResult, HerdrCli, _read_text


def test_read_text_extracts_agent_read_payload():
    output = _read_text(
        '{"id":"cli:agent:read","result":{"read":{"format":"text","pane_id":"w7:p1","text":"hello"}}}'
    )

    assert output == "hello"


def test_read_text_keeps_plain_text():
    assert _read_text("plain output") == "plain output"


def test_agent_prompt_uses_current_herdr_command(monkeypatch):
    cli = HerdrCli(HerdrCliConfig())
    calls = []

    def fake_run(args):
        calls.append(args)
        return CommandResult(stdout="", stderr="")

    monkeypatch.setattr(cli, "_run", fake_run)

    cli.agent_prompt("w7:p2", "hello")

    assert calls == [["agent", "prompt", "w7:p2", "hello"]]

