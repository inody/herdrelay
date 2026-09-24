from herdr_discord_bridge.discord_bot import (
    SPECIAL_KEY_SEQUENCES,
    SPECIAL_KEYS,
    _special_key_name,
)

# Key names Herdr 0.9 accepts for `pane send-keys` (checked against a live pane).
HERDR_SUPPORTED_KEYS = {"Escape", "Enter", "Tab", "Space", "Up", "Down", "Left", "Right", "C-c", "BS"}


def test_special_keys_only_use_names_herdr_supports():
    for keys in SPECIAL_KEYS.values():
        assert set(keys) <= HERDR_SUPPORTED_KEYS


def test_navigation_keys_herdr_lacks_are_sent_as_escape_sequences():
    assert _special_key_name("!pgup") == "pgup"
    assert SPECIAL_KEY_SEQUENCES["pgup"] == "\x1b[5~"
    assert SPECIAL_KEY_SEQUENCES["pgdn"] == "\x1b[6~"
    assert _special_key_name("!Home") == "home"
    assert _special_key_name("!end") == "end"


def test_special_key_name_ignores_normal_text():
    assert _special_key_name("hello") is None
    assert _special_key_name("!unknown") is None
    assert _special_key_name("!esc") == "esc"
