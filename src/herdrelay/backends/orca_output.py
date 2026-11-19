from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from ..config import AppConfig
from ..orca_client import OrcaClient
from ..store import Store

LOG = logging.getLogger(__name__)


class OrcaOutputSource:
    """Publish completed Orca responses into the shared versioned inbox."""

    def __init__(self, *, config: AppConfig, store: Store, client: OrcaClient):
        self.config = config
        self.store = store
        self.client = client
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        inbox = Path(self.config.streaming.hook_inbox_path).expanduser()
        await asyncio.to_thread(_prepare_inbox, inbox)
        LOG.info("Watching Orca agents for completed responses: %s", inbox)
        while not self._stopped.is_set():
            try:
                await self.poll_once(inbox)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Orca output polling failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.runtime.orca.refresh_seconds,
                )
            except TimeoutError:
                pass

    async def poll_once(self, inbox: Path | None = None) -> None:
        destination = inbox or Path(
            self.config.streaming.hook_inbox_path
        ).expanduser()
        await asyncio.to_thread(_prepare_inbox, destination)
        targets = await asyncio.to_thread(self.client.list_targets)
        for target in targets:
            raw_agent = target.raw.get("orca_agent") if target.raw else None
            if not isinstance(raw_agent, dict):
                continue
            session_id = raw_agent.get("orcaSessionId")
            state = raw_agent.get("state")
            text = raw_agent.get("lastAssistantMessage")
            updated_at = raw_agent.get("updatedAt")
            if not isinstance(session_id, str) or not session_id:
                continue
            if not isinstance(state, str) or not state:
                continue
            if updated_at is not None and (
                not isinstance(updated_at, int) or isinstance(updated_at, bool)
            ):
                continue
            text_value = text if isinstance(text, str) else ""
            key = self.client.target_key(target.target)
            fingerprint = _fingerprint(
                session_id=session_id,
                state=state,
                updated_at=updated_at,
                text=text_value,
            )
            state_key = _state_key(key.to_route_value())
            previous = self.store.get_state(state_key)
            if previous is None:
                self.store.set_state(state_key, fingerprint)
                continue
            if previous == fingerprint:
                continue
            if state.casefold() == "done" and text_value:
                payload = {
                    "version": 3,
                    "event_id": _event_id(
                        instance_id=key.instance_id or "",
                        address=key.address,
                        session_id=session_id,
                        updated_at=updated_at,
                        text=text_value,
                    ),
                    "backend": "orca",
                    "instance_id": key.instance_id,
                    "target": key.address,
                    "generation": session_id,
                    "agent": target.agent_name or "orca-agent",
                    "kind": "response",
                    "text": text_value,
                }
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > self.config.streaming.hook_max_event_bytes:
                    LOG.warning(
                        "Skipping oversized Orca response for %s (%s bytes)",
                        key.address,
                        len(encoded),
                    )
                else:
                    await asyncio.to_thread(
                        _write_event,
                        destination,
                        str(payload["event_id"]),
                        encoded,
                    )
            self.store.set_state(state_key, fingerprint)


def _prepare_inbox(inbox: Path) -> None:
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        inbox.chmod(0o700)
    except OSError:
        LOG.warning("Could not restrict hook inbox permissions: %s", inbox)


def _write_event(inbox: Path, event_id: str, encoded: bytes) -> None:
    path = inbox / f"{event_id}.json"
    if path.exists():
        return
    temporary = inbox / f".{event_id}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _state_key(route_target: str) -> str:
    digest = hashlib.sha256(route_target.encode("utf-8")).hexdigest()
    return f"orca-output:{digest}"


def _fingerprint(
    *, session_id: str, state: str, updated_at: int | None, text: str
) -> str:
    value = f"{session_id}\0{state.casefold()}\0{updated_at}\0{text}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_id(
    *,
    instance_id: str,
    address: str,
    session_id: str,
    updated_at: int | None,
    text: str,
) -> str:
    value = f"{instance_id}\0{address}\0{session_id}\0{updated_at}\0{text}"
    return f"orca-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
