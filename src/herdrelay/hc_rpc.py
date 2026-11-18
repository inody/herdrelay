from __future__ import annotations

import json
import os
import socket
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HcRuntimeConfig
from .domain import MutationOutcomeUnknownError
from .hc_protocol import (
    MAX_JSON_LINE_BYTES,
    PROTOCOL_GENERATION,
    HcHello,
    HcProtocolError,
    parse_hello_result,
)

DEFAULT_SESSION = "default"


class HcRpcError(RuntimeError):
    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


class HcRemoteError(HcRpcError):
    """A definitive error response returned by Core."""


class HcMutationOutcomeUnknown(HcRpcError, MutationOutcomeUnknownError):
    """Core may have applied a mutation whose response was not received."""


@dataclass(frozen=True)
class HcCallResult:
    hello: HcHello
    result: dict[str, Any]


class HcSocketLocator:
    """Resolve an existing Core socket without starting Core."""

    def __init__(self, config: HcRuntimeConfig):
        self.config = config

    def resolve(self) -> Path:
        explicit = self.config.socket_path or os.environ.get("HC_SOCKET")
        if explicit:
            return Path(explicit).expanduser()

        session = _normalize_session(os.environ.get("HC_SESSION"))
        state_home = os.environ.get("XDG_STATE_HOME")
        if state_home:
            state_root = Path(state_home).expanduser()
        else:
            home = os.environ.get("HOME")
            if not home:
                raise HcRpcError("HOME is unset and XDG_STATE_HOME was not provided")
            state_root = Path(home).expanduser() / ".local" / "state"
        state_dir = state_root / "herdr-core" / session

        lock_path = state_dir / "core.lock"
        try:
            raw_lock = lock_path.read_bytes()
        except FileNotFoundError:
            raw_lock = b""
        except OSError as exc:
            raise HcRpcError(f"cannot read Core lock file {lock_path}: {exc}") from exc
        if raw_lock:
            return Path(os.fsdecode(raw_lock))

        runtime_home = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_home:
            return Path(runtime_home).expanduser() / "herdr-core" / f"{session}.sock"
        run_user = Path(f"/run/user/{os.getuid()}")
        if run_user.is_dir():
            return run_user / "herdr-core" / f"{session}.sock"
        return state_dir / f"{session}.sock"


class HcRpcClient:
    """Plain JSON-RPC client for an already-running local Herdr Core."""

    def __init__(self, config: HcRuntimeConfig):
        self.config = config
        self.locator = HcSocketLocator(config)

    def call(self, method: str, params: dict[str, Any]) -> HcCallResult:
        path = self.locator.resolve()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.config.connect_timeout_seconds)
        try:
            connection.connect(str(path))
        except OSError as exc:
            connection.close()
            raise HcRpcError(
                f"cannot connect to existing Herdr Core socket {path}: {exc}"
            ) from exc

        try:
            connection.settimeout(self.config.request_timeout_seconds)
            try:
                connection.sendall(
                    encode_request(
                        request_id="herdrelay-hello",
                        method="hello",
                        params={"generation": PROTOCOL_GENERATION, "framed": False},
                    )
                )
                hello_payload = decode_response_line(
                    self._read_line(connection), expected_id="herdrelay-hello"
                )
                hello = parse_hello_result(hello_payload)
            except socket.timeout as exc:
                raise HcRpcError(
                    "Core handshake timed out after "
                    f"{self.config.request_timeout_seconds}s"
                ) from exc
            except HcProtocolError as exc:
                raise HcRpcError(str(exc)) from exc
            except OSError as exc:
                raise HcRpcError(f"Core handshake failed: {exc}") from exc

            mutation = method in {"terminal.send", "terminal.interrupt"}
            try:
                connection.sendall(
                    encode_request(
                        request_id="herdrelay-call",
                        method=method,
                        params=params,
                    )
                )
                result = decode_response_line(
                    self._read_line(connection), expected_id="herdrelay-call"
                )
                if not isinstance(result, dict):
                    raise HcRpcError(f"Core result for {method} must be an object")
                return HcCallResult(hello=hello, result=result)
            except HcRemoteError:
                raise
            except socket.timeout as exc:
                if mutation:
                    raise HcMutationOutcomeUnknown(
                        f"{method} outcome is unknown; inspect target state before retrying"
                    ) from exc
                raise HcRpcError(
                    f"Core request timed out after {self.config.request_timeout_seconds}s"
                ) from exc
            except HcRpcError as exc:
                if mutation:
                    raise HcMutationOutcomeUnknown(
                        f"{method} outcome is unknown; inspect target state before retrying"
                    ) from exc
                raise
            except OSError as exc:
                if mutation:
                    raise HcMutationOutcomeUnknown(
                        f"{method} outcome is unknown; inspect target state before retrying"
                    ) from exc
                raise HcRpcError(f"Core request failed: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _read_line(connection: socket.socket) -> bytes:
        chunks = bytearray()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                raise HcRpcError("Core closed the connection before replying")
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.extend(chunk[:newline])
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_JSON_LINE_BYTES:
                raise HcRpcError(
                    f"Core JSON line exceeds {MAX_JSON_LINE_BYTES} bytes"
                )
        if len(chunks) > MAX_JSON_LINE_BYTES:
            raise HcRpcError(f"Core JSON line exceeds {MAX_JSON_LINE_BYTES} bytes")
        return bytes(chunks)



def encode_request(
    *, request_id: str, method: str, params: dict[str, Any]
) -> bytes:
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(request, separators=(",", ":")).encode() + b"\n"


def decode_response_line(line: bytes | str, *, expected_id: str) -> Any:
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HcRpcError(f"Core returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HcRpcError("Core response must be an object")
    if payload.get("id") != expected_id:
        raise HcRpcError(
            f"Core response ID {payload.get('id')!r} did not match {expected_id!r}"
        )
    error = payload.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise HcRpcError("Core returned a malformed error")
        data = error.get("data")
        kind = data.get("kind") if isinstance(data, dict) else None
        message = error.get("message")
        raise HcRemoteError(
            str(message or "Core RPC failed"),
            kind=str(kind) if kind is not None else None,
        )
    if "result" not in payload:
        raise HcRpcError("Core response has neither result nor error")
    return payload["result"]


def _normalize_session(value: str | None) -> str:
    session = (value or DEFAULT_SESSION).strip() or DEFAULT_SESSION
    invalid = session in {".", ".."} or any(
        unicodedata.category(character).startswith("C")
        or character in r'/\:*?"<>|'
        for character in session
    )
    if invalid:
        raise HcRpcError(f"invalid HC_SESSION {session!r}")
    return session
