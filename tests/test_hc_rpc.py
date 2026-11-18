from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from herdr_discord_bridge.config import HcRuntimeConfig
from herdr_discord_bridge.domain import MutationOutcomeUnknownError
from herdr_discord_bridge.hc_rpc import HcRpcClient, HcRpcError, HcSocketLocator

ROOT = Path(__file__).parents[1]
HELLO = json.loads((ROOT / "tests/fixtures/hc/hello.json").read_text())


@pytest.fixture
def short_socket_path():
    directory = Path(tempfile.mkdtemp(prefix="hrpc.", dir="/tmp"))
    try:
        yield directory / "s"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def rpc_server(path: Path, responder):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    errors = []

    def serve():
        try:
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as reader:
                responder(connection, reader)
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=2)
        server.close()
        assert not thread.is_alive(), "test RPC server did not stop"
        assert not errors, errors


def send_response(connection, request_id, *, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    connection.sendall(json.dumps(payload).encode() + b"\n")


def test_rpc_client_handshakes_then_calls_method_on_existing_socket(
    short_socket_path,
):
    socket_path = short_socket_path
    requests = []

    def responder(connection, reader):
        hello_request = json.loads(reader.readline())
        requests.append(hello_request)
        send_response(connection, hello_request["id"], result=HELLO)
        call_request = json.loads(reader.readline())
        requests.append(call_request)
        send_response(
            connection,
            call_request["id"],
            result={"terminals": [], "hidden_finished": 0, "machines": []},
        )

    client = HcRpcClient(HcRuntimeConfig(socket_path=str(socket_path)))
    with rpc_server(socket_path, responder):
        result = client.call("terminal.list", {"all": False, "labels": {}})

    assert result.hello.core.id == "core-test-001"
    assert result.result["terminals"] == []
    assert requests[0]["method"] == "hello"
    assert requests[0]["params"] == {"generation": 1, "framed": False}
    assert requests[1]["method"] == "terminal.list"


def test_rpc_client_missing_socket_does_not_create_or_start_core(tmp_path):
    socket_path = tmp_path / "missing.sock"
    client = HcRpcClient(HcRuntimeConfig(socket_path=str(socket_path)))

    with pytest.raises(HcRpcError, match="cannot connect to existing"):
        client.call("terminal.list", {})

    assert not socket_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_rpc_client_preserves_core_error_kind(short_socket_path):
    socket_path = short_socket_path

    def responder(connection, reader):
        hello_request = json.loads(reader.readline())
        send_response(connection, hello_request["id"], result=HELLO)
        call_request = json.loads(reader.readline())
        send_response(
            connection,
            call_request["id"],
            error={
                "code": -32000,
                "message": "blocked",
                "data": {"kind": "blocked"},
            },
        )

    client = HcRpcClient(HcRuntimeConfig(socket_path=str(socket_path)))
    with rpc_server(socket_path, responder):
        with pytest.raises(HcRpcError, match="blocked") as caught:
            client.call("terminal.send", {"terminal": "t1", "text": "yes"})

    assert caught.value.kind == "blocked"


def test_rpc_client_times_out_without_retry(short_socket_path):
    socket_path = short_socket_path

    def responder(connection, reader):
        reader.readline()
        time.sleep(0.1)

    client = HcRpcClient(
        HcRuntimeConfig(
            socket_path=str(socket_path),
            connect_timeout_seconds=0.05,
            request_timeout_seconds=0.05,
        )
    )
    with rpc_server(socket_path, responder):
        with pytest.raises(HcRpcError, match="timed out"):
            client.call("terminal.list", {})


def test_mutation_timeout_is_outcome_unknown_and_is_not_retried(short_socket_path):
    socket_path = short_socket_path
    calls = []

    def responder(connection, reader):
        hello_request = json.loads(reader.readline())
        send_response(connection, hello_request["id"], result=HELLO)
        calls.append(json.loads(reader.readline()))
        time.sleep(0.1)

    client = HcRpcClient(
        HcRuntimeConfig(
            socket_path=str(socket_path),
            connect_timeout_seconds=0.05,
            request_timeout_seconds=0.05,
        )
    )
    with rpc_server(socket_path, responder):
        with pytest.raises(MutationOutcomeUnknownError, match="outcome is unknown"):
            client.call(
                "terminal.send",
                {"terminal": "t1", "text": "continue", "force": False},
            )

    assert len(calls) == 1



def test_socket_locator_prefers_explicit_then_hc_socket(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.sock"
    monkeypatch.setenv("HC_SOCKET", str(tmp_path / "env.sock"))

    assert HcSocketLocator(HcRuntimeConfig(socket_path=str(explicit))).resolve() == explicit
    assert HcSocketLocator(HcRuntimeConfig()).resolve() == tmp_path / "env.sock"


def test_socket_locator_reads_raw_core_lock_path(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    socket_path = tmp_path / "runtime" / "selected.sock"
    lock = state_home / "herdr-core" / "test-session" / "core.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(str(socket_path).encode())
    monkeypatch.delenv("HC_SOCKET", raising=False)
    monkeypatch.setenv("HC_SESSION", "test-session")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    assert HcSocketLocator(HcRuntimeConfig()).resolve() == socket_path


def test_socket_locator_rejects_unsafe_session(tmp_path, monkeypatch):
    monkeypatch.delenv("HC_SOCKET", raising=False)
    monkeypatch.setenv("HC_SESSION", "../default")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    with pytest.raises(HcRpcError, match="invalid HC_SESSION"):
        HcSocketLocator(HcRuntimeConfig()).resolve()
