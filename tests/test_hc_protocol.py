from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_discord_bridge.hc_protocol import (
    HcEventNotification,
    HcHeartbeatNotification,
    HcMissedNotification,
    HcProtocolError,
    HcUnknownNotification,
    decode_notification_line,
    inspect_openrpc_schema,
    parse_hello_result,
    parse_subscribe_result,
    parse_terminal,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hc"
SCRIPT = ROOT / "scripts" / "check_hc_compatibility.py"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_hello_accepts_generation_one_and_ignores_future_fields():
    hello = parse_hello_result(load_json("hello.json"))

    assert hello.generation == 1
    assert hello.version == "0.1.21"
    assert hello.core.id == "core-test-001"
    assert "events.subscribe" in hello.methods
    assert hello.features == frozenset({"future-feature"})


def test_hello_rejects_unsupported_generation_and_missing_method():
    payload = load_json("hello.json")
    payload["generation"] = 2
    with pytest.raises(HcProtocolError, match="unsupported Core protocol generation 2"):
        parse_hello_result(payload)

    payload = load_json("hello.json")
    payload["methods"].remove("terminal.send")
    with pytest.raises(HcProtocolError, match="terminal.send"):
        parse_hello_result(payload)


def test_hello_rejects_unexpected_framed_plain_handshake():
    payload = load_json("hello.json")
    payload["framed"] = True

    with pytest.raises(HcProtocolError, match="unexpectedly entered framed mode"):
        parse_hello_result(payload)

    assert parse_hello_result(payload, require_plain=False).framed is True


def test_subscribe_snapshot_decodes_agent_shell_worker_remote_and_finished():
    result = parse_subscribe_result(load_json("subscribe.json"))

    assert result.seq == 40
    assert result.channel is None
    assert [terminal.address for terminal in result.terminals] == [
        "t12",
        "t13",
        "t14",
        "remote/t7",
        "t15",
        "t16",
        "t9",
    ]
    visible = result.terminals[0]
    assert visible.status is not None
    assert visible.status.state == "working"
    assert visible.status.app == "pi"
    assert visible.status.message == "Reading tests"
    assert visible.records["tool"].title == "Running tests"
    assert visible.foreground is not None
    assert visible.foreground.argv == ("pi",)
    assert result.terminals[1].reported is False
    assert result.terminals[2].parent == "t12"
    assert result.terminals[3].address == "remote/t7"
    assert result.terminals[4].status is not None
    assert result.terminals[4].status.state == "idle"
    assert result.terminals[5].status is not None
    assert result.terminals[5].status.state == "error"
    assert result.terminals[6].state == "finished"


def test_terminal_requires_snapshot_fields_and_strict_types():
    payload = load_json("subscribe.json")["terminals"][0]
    payload.pop("cwd")
    with pytest.raises(HcProtocolError, match="cwd must be a string"):
        parse_terminal(payload)

    payload = load_json("subscribe.json")["terminals"][0]
    payload["turn"] = True
    with pytest.raises(HcProtocolError, match="turn must be an integer"):
        parse_terminal(payload)

    payload = load_json("subscribe.json")["terminals"][0]
    payload["status"]["progress"] = 300
    with pytest.raises(HcProtocolError, match="between 0 and 255"):
        parse_terminal(payload)


def test_notification_fixtures_cover_event_heartbeat_missed_and_unknown():
    notifications = [
        decode_notification_line(line)
        for line in (FIXTURES / "notifications.ndjson").read_bytes().splitlines()
    ]

    assert isinstance(notifications[0], HcEventNotification)
    assert notifications[0].kind == "status"
    assert notifications[0].terminal == "t12"
    assert notifications[0].data["status"]["state"] == "blocked"
    assert [notification.kind for notification in notifications[:8]] == [
        "status",
        "turn",
        "created",
        "restarted",
        "labels",
        "title",
        "finished",
        "removed",
    ]
    assert all(
        isinstance(notification, HcEventNotification)
        for notification in notifications[:8]
    )
    assert isinstance(notifications[8], HcHeartbeatNotification)
    assert notifications[8].seq == 48
    assert isinstance(notifications[9], HcMissedNotification)
    assert notifications[9].seq == 50
    assert notifications[9].terminals[0].turn == 4
    assert isinstance(notifications[10], HcUnknownNotification)
    assert notifications[10].method == "future.notification"


def test_notification_rejects_malformed_known_event_but_keeps_unknown_params():
    with pytest.raises(HcProtocolError, match="event params"):
        decode_notification_line(
            '{"jsonrpc":"2.0","method":"event","params":[]}'
        )
    with pytest.raises(HcProtocolError, match="terminals must be an array"):
        decode_notification_line(
            '{"jsonrpc":"2.0","method":"missed","params":{"seq":1}}'
        )

    unknown = decode_notification_line(
        '{"jsonrpc":"2.0","method":"future","params":[1,2,3]}'
    )
    assert isinstance(unknown, HcUnknownNotification)
    assert unknown.params == [1, 2, 3]


def test_schema_report_accepts_required_contract_and_reports_future_methods():
    report = inspect_openrpc_schema(
        load_json("schema-minimal.json"), cli_version="hc 0.1.21"
    )

    assert report.compatible is True
    assert report.cli_version == "hc 0.1.21"
    assert report.schema_version == "0.1.21"
    assert report.openrpc_version == "1.3.2"
    assert "future.method" in report.methods
    assert report.as_dict()["expected_protocol_generation"] == 1


def test_schema_report_lists_missing_methods_and_parameters():
    payload = load_json("schema-minimal.json")
    payload["methods"] = [
        method for method in payload["methods"] if method["name"] != "terminal.interrupt"
    ]
    send = next(
        method for method in payload["methods"] if method["name"] == "terminal.send"
    )
    send["params"] = [param for param in send["params"] if param["name"] != "text"]

    report = inspect_openrpc_schema(payload)

    assert report.compatible is False
    assert report.missing_methods == ("terminal.interrupt",)
    assert report.missing_params == {"terminal.send": ("text",)}


def test_static_compatibility_script_uses_only_version_and_schema(tmp_path):
    fake_hc = tmp_path / "hc"
    schema_path = FIXTURES / "schema-minimal.json"
    fake_hc.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('hc 0.1.21-test')\n"
        "elif sys.argv[1:] == ['schema']:\n"
        f"    print(pathlib.Path({str(schema_path)!r}).read_text())\n"
        "else:\n"
        "    raise SystemExit(99)\n"
    )
    fake_hc.chmod(0o755)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--hc", str(fake_hc), "--json"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["compatible"] is True
    assert result["cli_version"] == "hc 0.1.21-test"


def test_static_compatibility_script_reports_command_failure(tmp_path):
    fake_hc = tmp_path / "hc"
    fake_hc.write_text("#!/bin/sh\nexit 7\n")
    fake_hc.chmod(0o755)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--hc", str(fake_hc), "--json"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 2
    result = json.loads(completed.stdout)
    assert result["compatible"] is False
    assert "command failed" in result["error"]
