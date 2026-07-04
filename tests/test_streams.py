from herdr_discord_bridge.streams import compute_stream_diff


def test_diff_returns_everything_when_no_prev():
    assert compute_stream_diff(None, "line1\nline2") == "line1\nline2"
    assert compute_stream_diff("", "line1\nline2") == "line1\nline2"


def test_diff_returns_lines_after_last_meaningful_prev_line():
    prev = "old1\nold2\nold3"
    current = "old2\nold3\nnew1\nnew2"

    assert compute_stream_diff(prev, current) == "new1\nnew2"


def test_diff_ignores_trailing_blank_lines_in_marker():
    prev = "old1\nold2\n\n"
    current = "old2\nnew1"

    assert compute_stream_diff(prev, current) == "new1"


def test_diff_returns_full_current_when_marker_missing():
    prev = "completely\ndifferent"
    current = "new1\nnew2"

    assert compute_stream_diff(prev, current) == "new1\nnew2"


def test_diff_empty_when_current_ends_at_marker():
    prev = "old1\nold2"
    current = "old1\nold2"

    assert compute_stream_diff(prev, current) == ""
