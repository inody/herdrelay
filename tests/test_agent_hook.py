import json
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from agent_stop_hook import build_event, write_event  # noqa: E402
from manage_claude_hook import MARKER, update_settings  # noqa: E402


def stop_input(tmp_path, message="Finished response"):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    return {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "transcript_path": str(transcript),
        "last_assistant_message": message,
    }


def test_build_event_uses_herdr_pane_and_complete_message(tmp_path):
    event = build_event(stop_input(tmp_path), {"HERDR_PANE_ID": "w1:p2"})

    assert event is not None
    assert event["agent"] == "claude"
    assert event["pane_id"] == "w1:p2"
    assert event["text"] == "Finished response"
    assert len(event["event_id"]) == 64


def test_build_event_ignores_agent_outside_herdr(tmp_path):
    assert build_event(stop_input(tmp_path), {}) is None


def test_build_event_supports_codex_stop_payload(tmp_path):
    data = stop_input(tmp_path, "Codex response")
    data["turn_id"] = "turn-1"

    event = build_event(data, {"HERDR_PANE_ID": "w1:p3"}, agent="codex")

    assert event is not None
    assert event["agent"] == "codex"
    assert event["text"] == "Codex response"


def test_build_event_id_is_stable_for_repeated_stop_hook(tmp_path):
    data = stop_input(tmp_path)

    first = build_event(data, {"HERDR_PANE_ID": "w1:p2"})
    second = build_event(data, {"HERDR_PANE_ID": "w1:p2"})

    assert first is not None and second is not None
    assert first["event_id"] == second["event_id"]


def test_write_event_is_private_and_valid_json(tmp_path):
    event = build_event(stop_input(tmp_path), {"HERDR_PANE_ID": "w1:p2"})
    assert event is not None

    path = write_event(event, tmp_path / "inbox")

    assert json.loads(path.read_text())["text"] == "Finished response"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_install_and_uninstall_preserve_other_hooks(tmp_path):
    settings = {
        "model": "opus",
        "hooks": {
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo existing"}],
                }
            ]
        },
    }
    script = tmp_path / "agent_stop_hook.py"

    assert update_settings(settings, install=True, hook_script=script)
    assert len(settings["hooks"]["Stop"]) == 2
    command = settings["hooks"]["Stop"][1]["hooks"][0]["command"]
    assert MARKER in command
    assert not update_settings(settings, install=True, hook_script=script)
    assert update_settings(settings, install=False, hook_script=script)
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo existing"
