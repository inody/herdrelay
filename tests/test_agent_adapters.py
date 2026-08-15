from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from manage_codex_hook import install_text, uninstall_text  # noqa: E402


def test_codex_hook_install_preserves_notify_and_other_config(tmp_path):
    original = 'model = "gpt"\nnotify = ["existing", "turn-ended"]\n'
    script = tmp_path / "agent_stop_hook.py"

    installed = install_text(original, script)
    parsed = tomllib.loads(installed)

    assert parsed["notify"] == ["existing", "turn-ended"]
    assert parsed["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"
    assert "--agent codex" in parsed["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert install_text(installed, script) == installed
    assert uninstall_text(installed) == original


def test_codex_hook_install_merges_into_existing_hooks_table(tmp_path):
    original = '[hooks]\nSessionStart = []\n\n[features]\nhooks = true\n'

    installed = install_text(original, tmp_path / "agent_stop_hook.py")
    parsed = tomllib.loads(installed)

    assert parsed["hooks"]["SessionStart"] == []
    assert "Stop" in parsed["hooks"]
    assert parsed["features"]["hooks"] is True
    assert uninstall_text(installed) == original
