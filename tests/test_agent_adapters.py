from pathlib import Path
import subprocess
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


def test_pi_reinstall_keeps_prompt_override_removed(tmp_path):
    root = Path(__file__).parents[1]
    source = root / "integrations/pi-herdrelay-output.ts"
    target = tmp_path / "herdrelay-output.ts"
    previous = "// HERDRELAY_ADAPTER_ID=pi-output-v1\n// locally removed prompt override\n"
    target.write_text(previous)
    command = [sys.executable, str(root / "scripts/manage_pi_adapter.py"), "install", "--target", str(target)]

    subprocess.run(command, check=True, capture_output=True, timeout=5)

    assert target.read_text() == source.read_text()
    assert "before_agent_start" not in target.read_text()
    assert 'ctx.mode !== "tui"' in target.read_text()
    backups = list(tmp_path.glob("*.herdrelay-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == previous
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
    assert "already installed" in result.stdout
    assert len(list(tmp_path.glob("*.herdrelay-backup-*"))) == 1


def test_codex_hook_install_merges_into_existing_hooks_table(tmp_path):
    original = '[hooks]\nSessionStart = []\n\n[features]\nhooks = true\n'

    installed = install_text(original, tmp_path / "agent_stop_hook.py")
    parsed = tomllib.loads(installed)

    assert parsed["hooks"]["SessionStart"] == []
    assert "Stop" in parsed["hooks"]
    assert parsed["features"]["hooks"] is True
    assert uninstall_text(installed) == original
