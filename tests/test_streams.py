from unittest.mock import AsyncMock
import json

import pytest

from herdr_discord_bridge.config import AppConfig, StreamConfig
from herdr_discord_bridge.models import PendingQuestion, QuestionOption
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.streams import (
    StreamManager,
    _hook_event_text,
    _last_lines,
    compute_stream_diff,
    load_hook_output_event,
)


def test_diff_returns_everything_when_no_prev():
    assert compute_stream_diff(None, "line1\nline2") == "line1\nline2"
    assert compute_stream_diff("", "line1\nline2") == "line1\nline2"


def test_diff_returns_lines_after_last_meaningful_prev_line():
    prev = "old1\nold2\nold3"
    current = "old2\nold3\nnew1\nnew2"

    assert compute_stream_diff(prev, current) == "new1\nnew2"


def test_diff_does_not_drop_output_after_only_a_weak_one_line_overlap():
    prev = "old1\nold2\n\n"
    current = "old2\nnew1"

    assert compute_stream_diff(prev, current) == current


def test_diff_returns_full_current_when_marker_missing():
    prev = "completely\ndifferent"
    current = "new1\nnew2"

    assert compute_stream_diff(prev, current) == "new1\nnew2"


def test_diff_empty_when_snapshots_are_equal():
    prev = "old1\nold2"
    current = "old1\nold2"

    assert compute_stream_diff(prev, current) == ""


def test_diff_uses_multi_line_overlap_instead_of_repeated_footer_marker():
    footer = "────────────────────────────────"
    prev = f"intro\n{footer}\nmiddle\n{footer}\nstatus"
    current = f"middle\n{footer}\nnew1\nnew2\n{footer}\nstatus"

    assert compute_stream_diff(prev, current) == f"new1\nnew2\n{footer}\nstatus"


def test_diff_preserves_all_new_lines_from_large_rolling_snapshot():
    prev = "\n".join(f"old-{i}" for i in range(1000))
    current = "\n".join(
        [*(f"old-{i}" for i in range(400, 1000)), *(f"new-{i}" for i in range(400))]
    )

    assert compute_stream_diff(prev, current) == "\n".join(
        f"new-{i}" for i in range(400)
    )


def test_initial_backfill_keeps_only_configured_number_of_lines():
    assert _last_lines("one\ntwo\nthree\nfour", 2) == "three\nfour"


def test_load_hook_output_event(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "turn-1",
                "agent": "claude",
                "pane_id": "w1:p1",
                "text": "complete response",
            }
        )
    )

    event = load_hook_output_event(path, max_bytes=1000)

    assert event.event_id == "turn-1"
    assert event.agent == "claude"
    assert event.pane_id == "w1:p1"
    assert event.text == "complete response"


def test_question_hook_event_is_labeled_for_discord(tmp_path):
    path = tmp_path / "question.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "question-1",
                "agent": "claude",
                "pane_id": "w1:p1",
                "kind": "question",
                "question": {
                    "prompt": "Which option?",
                    "options": [{"label": "One"}, {"label": "Two"}],
                    "multi_select": False,
                },
                "text": "Which option?",
            }
        )
    )

    event = load_hook_output_event(path, max_bytes=1000)

    assert _hook_event_text(event) == "[Claude asks]\nWhich option?"


def test_load_hook_output_event_rejects_oversize_file(tmp_path):
    path = tmp_path / "event.json"
    path.write_text("{}")

    with pytest.raises(ValueError, match="exceeds"):
        load_hook_output_event(path, max_bytes=1)


@pytest.mark.asyncio
async def test_hook_event_is_delivered_once_and_removed(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    event_path = inbox / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "version": 1,
                "event_id": "turn-1",
                "agent": "claude",
                "pane_id": "w1:p1",
                "text": "complete response",
            }
        )
    )
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_agent_thread(
        pane_id="w1:p1", thread_id=123, guild_id=456, alias="agent"
    )
    manager = StreamManager(
        bot=None,  # type: ignore[arg-type]
        config=AppConfig(
            discord_token="token",
            streaming=StreamConfig(mode="hooks", hook_inbox_path=str(inbox)),
        ),
        store=store,
        client=None,  # type: ignore[arg-type]
    )
    thread = AsyncMock()
    monkeypatch.setattr(manager, "_fetch_thread", AsyncMock(return_value=thread))

    await manager._drain_hook_events(inbox)

    assert not event_path.exists()
    assert store.has_event_key("agent-output:claude:turn-1")
    thread.send.assert_awaited_once()
    assert "complete response" in thread.send.await_args.args[0]


def _hook_manager(tmp_path, inbox, store):
    return StreamManager(
        bot=None,  # type: ignore[arg-type]
        config=AppConfig(
            discord_token="token",
            streaming=StreamConfig(mode="hooks", hook_inbox_path=str(inbox)),
        ),
        store=store,
        client=None,  # type: ignore[arg-type]
    )


def _pending(pane_id="w1:p1", event_id="question-1"):
    return PendingQuestion(
        pane_id=pane_id,
        event_id=event_id,
        prompt="Which option?",
        options=(QuestionOption("One"), QuestionOption("Two")),
    )


def _write_event(path, mtime_ns, **fields):
    import os

    path.write_text(json.dumps({"version": 1, "agent": "claude", **fields}))
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.mark.asyncio
async def test_resolution_event_clears_pending_question_without_posting(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_pending_question(_pending())
    _write_event(
        inbox / "resolved.json",
        1_000,
        event_id="resolved-1",
        pane_id="w1:p1",
        kind="question_resolved",
    )
    manager = _hook_manager(tmp_path, inbox, store)
    fetch_thread = AsyncMock()
    monkeypatch.setattr(manager, "_fetch_thread", fetch_thread)

    await manager._drain_hook_events(inbox)

    assert store.get_pending_question("w1:p1") is None
    fetch_thread.assert_not_awaited()
    assert list(inbox.iterdir()) == []


@pytest.mark.asyncio
async def test_hook_events_are_processed_in_arrival_order(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_agent_thread(pane_id="w1:p1", thread_id=123, guild_id=456, alias="agent")
    store.upsert_pending_question(_pending(event_id="old-question"))
    # Name order is the reverse of arrival order: the resolution of the old
    # question arrived first, then Claude asked a new one.
    _write_event(
        inbox / "b-resolved.json",
        1_000,
        event_id="resolved-1",
        pane_id="w1:p1",
        kind="question_resolved",
    )
    _write_event(
        inbox / "a-question.json",
        2_000,
        event_id="new-question",
        pane_id="w1:p1",
        kind="question",
        text="Which option?",
        question={"prompt": "Which option?", "options": [{"label": "One"}]},
    )
    manager = _hook_manager(tmp_path, inbox, store)
    monkeypatch.setattr(manager, "_fetch_thread", AsyncMock(return_value=AsyncMock()))

    await manager._drain_hook_events(inbox)

    pending = store.get_pending_question("w1:p1")
    assert pending is not None
    assert pending.event_id == "new-question"
