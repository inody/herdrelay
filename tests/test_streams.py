from unittest.mock import AsyncMock
import json

import pytest

from herdr_discord_bridge.config import AppConfig, StreamConfig
from herdr_discord_bridge.store import Store
from herdr_discord_bridge.streams import (
    StreamManager,
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
