from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from .config import AppConfig, effective_general_provider, load_config

LOG = logging.getLogger(__name__)
GATEWAY_VERSION = 1
MAX_GATEWAY_FILE_BYTES = 1_048_576


class GatewayOutcomeUnknown(RuntimeError):
    pass


class GatewayQueue:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.requests = self.root / "requests"
        self.processing = self.root / "processing"
        self.pending = self.root / "pending"
        self.events = self.root / "events"
        self.heartbeat = self.root / "heartbeat.json"
        self.legacy_chat_state = self.root / "chat.json"
        self.chat_state = self.root / "chat.telegram.json"
        self.lock = self.root / "worker.lock"

    def chat_state_for(self, transport: str) -> Path:
        if transport == "telegram":
            return self.chat_state
        if transport != "discord":
            raise ValueError(f"unsupported gateway transport: {transport!r}")
        return self.root / "chat.discord.json"

    def prepare(self) -> None:
        for path in (
            self.root,
            self.requests,
            self.processing,
            self.pending,
            self.events,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def enqueue(self, request: dict[str, Any]) -> Path:
        self.prepare()
        request_id = _safe_id(request.get("request_id"))
        destination = self.requests / f"{request_id}.json"
        if destination.exists():
            return destination
        _write_json(destination, request)
        return destination

    def event_paths(self) -> tuple[Path, ...]:
        self.prepare()
        return tuple(
            sorted(
                self.events.glob("*.json"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
        )


class OrcaBotGatewayWorker:
    def __init__(self, config: AppConfig):
        self.config = config
        self.runtime = config.runtime.orca
        self.bot = config.telegram.general_router.orca_bot
        self.queue = GatewayQueue(self.bot.queue_path)
        self.handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
        self.pane_key = os.environ.get("ORCA_PANE_KEY", "").strip()
        if not self.handle:
            raise RuntimeError("Orca bot gateway requires ORCA_TERMINAL_HANDLE")

    def run_forever(self) -> None:
        self.queue.prepare()
        with self.queue.lock.open("a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another Orca bot gateway worker is active") from exc
            self._recover_processing()
            LOG.info("Orca bot gateway active as %s", self.handle)
            while True:
                self._write_heartbeat()
                self._process_requests()
                self._process_replies()
                time.sleep(self.bot.refresh_seconds)

    def _recover_processing(self) -> None:
        for path in self.queue.processing.glob("*.json"):
            try:
                request = _read_json(path)
                request_id = _safe_id(request.get("request_id"))
            except Exception as exc:
                LOG.warning("Discarding invalid gateway processing file %s: %s", path, exc)
                path.unlink(missing_ok=True)
                continue
            if (self.queue.pending / f"{request_id}.json").exists():
                path.unlink(missing_ok=True)
                continue
            self._write_event(
                request_id=request_id,
                suffix="outcome-unknown",
                payload={
                    **_event_base(request),
                    "kind": "outcome_unknown",
                    "detail": "gateway restarted after mutation processing began",
                },
            )
            path.unlink(missing_ok=True)

    def _process_requests(self) -> None:
        paths = sorted(
            self.queue.requests.glob("*.json"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        for source in paths:
            processing = self.queue.processing / source.name
            try:
                source.replace(processing)
            except FileNotFoundError:
                continue
            try:
                request = _read_json(processing)
                request_id = _safe_id(request.get("request_id"))
                kind = request.get("kind")
                if kind == "reset":
                    transport = _transport(request)
                    self.queue.chat_state_for(transport).unlink(missing_ok=True)
                    if transport == "telegram":
                        self.queue.legacy_chat_state.unlink(missing_ok=True)
                    self._write_event(
                        request_id=request_id,
                        suffix="reset",
                        payload={**_event_base(request), "kind": "reset"},
                    )
                elif kind == "send":
                    self._send_request(request)
                else:
                    raise ValueError(f"unsupported gateway request kind: {kind!r}")
            except Exception as exc:
                LOG.exception("Orca bot gateway request failed: %s", processing.name)
                try:
                    request_id = _safe_id(request.get("request_id"))
                    payload = _event_base(request)
                except Exception:
                    request_id = processing.stem
                    payload = {"request_id": request_id}
                outcome_unknown = isinstance(exc, GatewayOutcomeUnknown)
                self._write_event(
                    request_id=request_id,
                    suffix=("outcome-unknown" if outcome_unknown else "failed"),
                    payload={
                        **payload,
                        "kind": (
                            "outcome_unknown" if outcome_unknown else "send_failed"
                        ),
                        "detail": str(exc),
                    },
                )
            finally:
                processing.unlink(missing_ok=True)

    def _send_request(self, request: dict[str, Any]) -> None:
        request_id = _safe_id(request.get("request_id"))
        text = request.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("gateway request text must be non-empty")
        transport = _transport(request)
        chat_id = self._chat_id(transport)
        body_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f"{request_id}-", suffix=".txt", dir=self.queue.processing
            )
            body_path = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            body_path.chmod(0o600)
            chat_selector = chat_id or "new"
            payload = self._orca_json(
                [
                    "orchestration",
                    "send",
                    "--from",
                    self.handle,
                    "--to",
                    f"bot:{self.bot.bot_id}",
                    "--chat",
                    chat_selector,
                    "--type",
                    "handoff",
                    "--subject",
                    _subject(request),
                    "--body-file",
                    str(body_path),
                    "--full",
                ],
                mutation=True,
            )
        finally:
            if body_path is not None:
                body_path.unlink(missing_ok=True)

        result = _object(payload.get("result"), "Orca bot send result")
        message = _object(result.get("message"), "Orca bot message")
        outbound_id = _string(message, "id", "Orca bot message ID")
        to_handle = _string(message, "to_handle", "Orca bot destination")
        if chat_id is None:
            prefix = "orca_session_id:"
            if not to_handle.startswith(prefix) or len(to_handle) <= len(prefix):
                raise ValueError("Orca did not return the new bot chat identity")
            chat_id = to_handle[len(prefix) :]
            _write_json(
                self.queue.chat_state_for(transport),
                {
                    "version": GATEWAY_VERSION,
                    "bot_id": self.bot.bot_id,
                    "chat_id": chat_id,
                },
            )
            self._rename_chat_best_effort(chat_id, request)
        pending = {
            **_event_base(request),
            "version": GATEWAY_VERSION,
            "outbound_message_id": outbound_id,
            "bot_chat_id": chat_id,
        }
        _write_json(self.queue.pending / f"{request_id}.json", pending)
        self._write_event(
            request_id=request_id,
            suffix="sent",
            payload={**pending, "kind": "sent"},
        )

    def _rename_chat_best_effort(
        self, chat_id: str, request: dict[str, Any]
    ) -> None:
        try:
            self._orca_json(
                [
                    "chat",
                    "rename",
                    "--session",
                    chat_id,
                    "--name",
                    _subject(request),
                ],
                mutation=True,
            )
        except Exception:
            LOG.exception("Could not rename Orca Bot chat %s", chat_id)

    def _process_replies(self) -> None:
        try:
            payload = self._orca_json(
                [
                    "orchestration",
                    "check",
                    "--terminal",
                    self.handle,
                    "--peek",
                    "--full",
                ]
            )
        except Exception as exc:
            LOG.warning("Could not peek Orca bot gateway mailbox: %s", exc)
            return
        result = _object(payload.get("result"), "Orca mailbox result")
        messages = result.get("messages")
        if not isinstance(messages, list) or not messages:
            return
        pending_by_message: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in self.queue.pending.glob("*.json"):
            try:
                pending = _read_json(path)
                outbound_id = _string(
                    pending, "outbound_message_id", "pending outbound message ID"
                )
            except Exception as exc:
                LOG.warning("Discarding invalid gateway pending file %s: %s", path, exc)
                path.unlink(missing_ok=True)
                continue
            pending_by_message[outbound_id] = (path, pending)

        for raw in messages:
            if not isinstance(raw, dict):
                continue
            thread_id = raw.get("thread_id")
            body = raw.get("body")
            if not isinstance(thread_id, str) or not isinstance(body, str):
                continue
            matched = pending_by_message.get(thread_id)
            if matched is None:
                LOG.warning("Ignoring unmatched gateway mailbox reply %s", raw.get("id"))
                continue
            pending_path, pending = matched
            request_id = _safe_id(pending.get("request_id"))
            self._write_event(
                request_id=request_id,
                suffix="reply",
                payload={
                    **pending,
                    "kind": "reply",
                    "reply_message_id": raw.get("id"),
                    "text": body,
                },
            )
            pending_path.unlink(missing_ok=True)

        try:
            self._orca_json(
                [
                    "orchestration",
                    "check",
                    "--terminal",
                    self.handle,
                    "--full",
                ],
                mutation=True,
            )
        except Exception as exc:
            LOG.warning("Could not consume Orca bot gateway mailbox: %s", exc)

    def _chat_id(self, transport: str) -> str | None:
        state_path = self.queue.chat_state_for(transport)
        try:
            state = _read_json(state_path)
        except FileNotFoundError:
            if transport != "telegram":
                return None
            try:
                state = _read_json(self.queue.legacy_chat_state)
            except FileNotFoundError:
                return None
        if state.get("bot_id") != self.bot.bot_id:
            return None
        value = state.get("chat_id")
        return value if isinstance(value, str) and value else None

    def _write_heartbeat(self) -> None:
        _write_json(
            self.queue.heartbeat,
            {
                "version": GATEWAY_VERSION,
                "handle": self.handle,
                "pane_key": self.pane_key,
                "bot_id": self.bot.bot_id,
                "gateway_worktree": self.bot.gateway_worktree,
                "pid": os.getpid(),
                "updated_at": time.time(),
            },
        )

    def _write_event(
        self, *, request_id: str, suffix: str, payload: dict[str, Any]
    ) -> None:
        _write_json(self.queue.events / f"{request_id}.{suffix}.json", payload)

    def _orca_json(
        self, args: list[str], *, mutation: bool = False
    ) -> dict[str, Any]:
        return run_orca_json(
            cli_path=self.runtime.cli_path,
            environment=self.runtime.environment,
            timeout_seconds=self.bot.command_timeout_seconds,
            args=args,
            mutation=mutation,
        )


def run_orca_json(
    *,
    cli_path: str,
    environment: str,
    timeout_seconds: float,
    args: list[str],
    mutation: bool = False,
) -> dict[str, Any]:
    command = [cli_path, *args, "--environment", environment, "--json"]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"Orca command timed out: {' '.join(command)}"
        if mutation:
            raise GatewayOutcomeUnknown(detail) from exc
        raise RuntimeError(detail) from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Orca did not return JSON: {detail}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Orca response must be an object")
    if completed.returncode != 0 or payload.get("ok") is not True:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        detail = str(message or completed.stderr.strip() or "Orca command failed")
        if mutation and code in {"outcome_unknown", "request_outcome_unknown"}:
            raise GatewayOutcomeUnknown(detail)
        raise RuntimeError(detail)
    return payload


def _event_base(request: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "request_id",
        "audit_id",
        "event_key",
        "transport",
        "chat_id",
        "thread_id",
        "user_id",
        "message_id",
        "guild_id",
        "channel_id",
        "source_kind",
    )
    return {key: request.get(key) for key in keys}


def _transport(request: dict[str, Any]) -> str:
    transport = request.get("transport") or "telegram"
    if transport not in {"telegram", "discord"}:
        raise ValueError(f"unsupported gateway transport: {transport!r}")
    return str(transport)


def _subject(request: dict[str, Any]) -> str:
    return {
        "telegram": "Telegram Chat",
        "discord": "Discord Chat",
    }[_transport(request)]


def _safe_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("gateway request ID must be a non-empty string")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if any(character not in allowed for character in value):
        raise ValueError("gateway request ID contains unsupported characters")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        encoded = handle.read(MAX_GATEWAY_FILE_BYTES + 1)
    if len(encoded) > MAX_GATEWAY_FILE_BYTES:
        raise ValueError(f"gateway file exceeds {MAX_GATEWAY_FILE_BYTES} bytes")
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("gateway file must contain an object")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_GATEWAY_FILE_BYTES:
        raise ValueError("gateway payload is too large")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _string(item: dict[str, Any], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Orca Bot gateway worker.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config, require_discord_token=False)
    if effective_general_provider(config) != "orca-bot":
        raise ValueError("Telegram General provider is not configured as orca-bot")
    OrcaBotGatewayWorker(config).run_forever()


if __name__ == "__main__":
    main()
