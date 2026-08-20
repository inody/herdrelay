from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import time
from collections.abc import Callable

import discord

from .config import AppConfig
from .formatter import split_tail_chunks, wrap_code_block
from .herdr_client import HerdrClient
from .models import HerdrTarget, PendingQuestion, QuestionOption
from .store import Store

LOG = logging.getLogger(__name__)
QUESTION_VIEW_FACTORY = Callable[[PendingQuestion], discord.ui.View | None]


def compute_stream_diff(prev_tail: str | None, current_tail: str) -> str:
    """Return lines added after the overlap between two rolling snapshots.

    Herdr returns the most recent N lines, so a later snapshot normally starts
    somewhere inside the previous one.  Matching a multi-line prefix avoids
    confusing repeated TUI separators and footer lines for the stream cursor.
    """
    if not prev_tail:
        return current_tail
    if prev_tail == current_tail:
        return ""
    prev_lines = prev_tail.split("\n")
    curr_lines = current_tail.split("\n")
    overlap = _longest_prefix_overlap(prev_lines, curr_lines)
    if overlap >= 2:
        return "\n".join(curr_lines[overlap:])
    # Duplicating a line is safer than dropping new output after a weak,
    # potentially coincidental one-line match.
    return current_tail


def _longest_prefix_overlap(previous: list[str], current: list[str]) -> int:
    """Length of the longest current prefix appearing contiguously in previous."""
    if not current:
        return 0
    best = 0
    first = current[0]
    for start, line in enumerate(previous):
        if line != first:
            continue
        size = 1
        limit = min(len(previous) - start, len(current))
        while size < limit and previous[start + size] == current[size]:
            size += 1
        if size > best:
            best = size
            if best == len(current):
                break
    return best


def _last_lines(text: str, count: int) -> str:
    if count <= 0:
        return ""
    return "\n".join(text.split("\n")[-count:])


@dataclass(frozen=True)
class HookOutputEvent:
    event_id: str
    agent: str
    pane_id: str
    text: str
    kind: str = "response"
    question: PendingQuestion | None = None


def load_hook_output_event(path: Path, *, max_bytes: int) -> HookOutputEvent:
    """Load and validate one agent-adapter output event."""
    if path.stat().st_size > max_bytes:
        raise ValueError(f"event exceeds {max_bytes} bytes")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported event format")
    values = {
        key: payload.get(key) for key in ("event_id", "agent", "pane_id", "text")
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ValueError("event_id, agent, pane_id, and text must be non-empty strings")
    if len(values["event_id"]) > 200 or len(values["agent"]) > 50:
        raise ValueError("event identifier or agent name is too long")
    kind = payload.get("kind", "response")
    if kind not in {"response", "question"}:
        raise ValueError("unsupported event kind")
    question = _load_question(payload.get("question"), pane_id=values["pane_id"], event_id=values["event_id"])
    if kind == "question" and question is None:
        raise ValueError("question event is missing a valid question")
    return HookOutputEvent(**values, kind=kind, question=question)


def _load_question(
    value: object, *, pane_id: str, event_id: str
) -> PendingQuestion | None:
    if not isinstance(value, dict):
        return None
    prompt = value.get("prompt")
    options_data = value.get("options")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(options_data, list):
        return None
    options = tuple(
        QuestionOption(
            label=option["label"].strip(),
            description=(
                option["description"].strip()
                if isinstance(option.get("description"), str)
                and option["description"].strip()
                else None
            ),
        )
        for option in options_data
        if isinstance(option, dict)
        and isinstance(option.get("label"), str)
        and option["label"].strip()
    )
    if not options:
        return None
    return PendingQuestion(
        pane_id=pane_id,
        event_id=event_id,
        prompt=prompt.strip(),
        options=options,
        multi_select=value.get("multi_select") is True,
    )


def _hook_event_text(event: HookOutputEvent) -> str:
    if event.kind == "question":
        return f"[Claude asks]\n{event.text}"
    return event.text


class StreamManager:
    """Relay adapter events or optional polled output to bound Discord threads."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        config: AppConfig,
        store: Store,
        client: HerdrClient,
        question_view_factory: QUESTION_VIEW_FACTORY | None = None,
    ):
        self.bot = bot
        self.config = config
        self.store = store
        self.client = client
        self.question_view_factory = question_view_factory
        self._stopped = asyncio.Event()
        self._prev_recent: dict[str, str] = {}
        self._prev_visible: dict[str, str] = {}
        self._read_failures: set[str] = set()

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        if self.config.streaming.mode == "hooks":
            await self._run_hook_forever()
        else:
            await self._run_poll_forever()

    async def _run_poll_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Stream sync failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.streaming.refresh_seconds,
                )
            except TimeoutError:
                pass

    async def _run_hook_forever(self) -> None:
        inbox = Path(self.config.streaming.hook_inbox_path).expanduser()
        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            inbox.chmod(0o700)
        except OSError:
            LOG.warning("Could not restrict hook inbox permissions: %s", inbox)
        LOG.info("Watching agent hook inbox %s", inbox)
        while not self._stopped.is_set():
            try:
                await self._drain_hook_events(inbox)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Agent hook inbox scan failed")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.config.streaming.hook_refresh_seconds,
                )
            except TimeoutError:
                pass

    async def _drain_hook_events(self, inbox: Path) -> None:
        paths = await asyncio.to_thread(lambda: sorted(inbox.glob("*.json")))
        for path in paths:
            try:
                event = await asyncio.to_thread(
                    load_hook_output_event,
                    path,
                    max_bytes=self.config.streaming.hook_max_event_bytes,
                )
            except FileNotFoundError:
                continue
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                LOG.warning("Discarding invalid agent hook event %s: %s", path.name, exc)
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            dedupe_key = f"agent-output:{event.agent}:{event.event_id}"
            if self.store.has_event_key(dedupe_key):
                await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            thread_id = self.store.get_agent_thread(event.pane_id)
            if thread_id is None:
                try:
                    age = time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age >= self.config.streaming.hook_max_event_age_seconds:
                    LOG.warning(
                        "Discarding expired %s output for unbound pane %s",
                        event.agent,
                        event.pane_id,
                    )
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                continue

            thread = await self._fetch_thread(int(thread_id))
            if thread is None:
                continue
            if event.kind == "question" and event.question is not None:
                self.store.upsert_pending_question(event.question)
                view = (
                    self.question_view_factory(event.question)
                    if self.question_view_factory
                    else None
                )
                delivered = await self._send_text(
                    thread, thread_id, _hook_event_text(event), view=view
                )
            else:
                delivered = await self._send_diff(thread, thread_id, event.text)
            if not delivered:
                continue
            self.store.mark_event_seen(dedupe_key)
            if event.kind == "response":
                self.store.clear_pending_question(event.pane_id)
            await asyncio.to_thread(path.unlink, missing_ok=True)
            LOG.info("Delivered %s hook output for pane %s", event.agent, event.pane_id)

    async def sync(self) -> None:
        targets = await asyncio.to_thread(self.client.list_targets)
        active_pane_ids = {target.target for target in targets if target.target}
        self._forget_inactive_panes(active_pane_ids)
        for target in targets:
            if not target.target:
                continue
            try:
                await self._stream_target(target)
            except Exception:
                LOG.exception("Failed to stream %s", target.target)

    def _forget_inactive_panes(self, active_pane_ids: set[str]) -> None:
        for state in (self._prev_recent, self._prev_visible):
            for pane_id in set(state) - active_pane_ids:
                del state[pane_id]
        self._read_failures.intersection_update(active_pane_ids)

    async def _stream_target(self, target: HerdrTarget) -> None:
        pane_id = target.target
        thread_id = self.store.get_agent_thread(pane_id)
        if thread_id is None:
            return
        thread = await self._fetch_thread(int(thread_id))
        if thread is None:
            return
        tail_lines = self.config.streaming.tail_lines
        try:
            current_recent = await asyncio.to_thread(
                self.client.read,
                pane_id,
                lines=tail_lines,
                fmt="text",
                source="recent",
            )
        except Exception as exc:
            if pane_id not in self._read_failures:
                LOG.warning("Failed to read pane %s for streaming: %s", pane_id, exc)
                self._read_failures.add(pane_id)
            return
        if pane_id in self._read_failures:
            LOG.info("Pane read recovered for streaming %s", pane_id)
            self._read_failures.remove(pane_id)
        previous_recent = self._prev_recent.get(pane_id)
        self._prev_recent[pane_id] = current_recent
        if previous_recent is None:
            # Keep restart backfill small even though ongoing snapshots are large.
            # Seed visible at the same time to avoid reposting the same screen on
            # the next cycle when recent output has not changed.
            if self.config.streaming.enable_visible_fallback:
                await self._seed_visible(pane_id, tail_lines)
            diff = _last_lines(current_recent, self.config.streaming.initial_tail_lines)
        else:
            diff = compute_stream_diff(previous_recent, current_recent)
        if diff and diff.strip():
            await self._send_diff(thread, thread_id, diff)
            return
        if not self.config.streaming.enable_visible_fallback:
            return
        # No change in agent output — check the visible screen for pager/TUI
        # updates (e.g. Claude's /usage) that recent does not capture.
        try:
            current_visible = await asyncio.to_thread(
                self.client.read,
                pane_id,
                lines=tail_lines,
                fmt="text",
                source="visible",
            )
        except Exception as exc:
            if pane_id not in self._read_failures:
                LOG.warning(
                    "Failed to read visible pane %s for streaming: %s", pane_id, exc
                )
                self._read_failures.add(pane_id)
            return
        previous_visible = self._prev_visible.get(pane_id)
        self._prev_visible[pane_id] = current_visible
        if previous_visible is None:
            return
        diff = compute_stream_diff(previous_visible, current_visible)
        if diff and diff.strip():
            await self._send_diff(thread, thread_id, diff)

    async def _seed_visible(self, pane_id: str, tail_lines: int) -> None:
        try:
            self._prev_visible[pane_id] = await asyncio.to_thread(
                self.client.read,
                pane_id,
                lines=tail_lines,
                fmt="text",
                source="visible",
            )
        except Exception:
            LOG.exception("Failed to seed visible tail for streaming %s", pane_id)

    async def _send_diff(
        self, thread: discord.Thread, thread_id: int | str, diff: str
    ) -> bool:
        return await self._send_text(thread, thread_id, diff)

    async def _send_text(
        self,
        thread: discord.Thread,
        thread_id: int | str,
        text: str,
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        for index, chunk in enumerate(
            split_tail_chunks(text, max_chars=self.config.max_output_chars)
        ):
            try:
                await thread.send(wrap_code_block(chunk), view=view if index == 0 else None)
            except discord.HTTPException:
                LOG.warning("Failed to post stream chunk to thread %s", thread_id)
                return False
        return True

    async def _fetch_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except Exception:
            return None
        return fetched if isinstance(fetched, discord.Thread) else None
