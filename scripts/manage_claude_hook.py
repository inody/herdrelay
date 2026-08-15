#!/usr/bin/env python3
"""Install or remove HerdRelay's Claude Code Stop hook."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

MARKER = "herdrelay-claude-stop-hook"
DEFAULT_SETTINGS = Path("~/.claude/settings.json").expanduser()


def hook_entry(hook_script: Path) -> dict[str, object]:
    command = (
        f"/usr/bin/env python3 {shlex.quote(str(hook_script))} --agent claude"
        f" # {MARKER}"
    )
    return {
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": 5}],
    }


def is_herdrelay_group(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(hook, dict) and MARKER in str(hook.get("command", ""))
        for hook in hooks
    )


def update_settings(
    settings: dict[str, object], *, install: bool, hook_script: Path
) -> bool:
    hooks = settings.get("hooks")
    if hooks is None:
        if not install:
            return False
        hooks = {}
        settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("settings.json field 'hooks' must be an object")
    stop = hooks.get("Stop")
    if stop is None:
        if not install:
            return False
        stop = []
        hooks["Stop"] = stop
    if not isinstance(stop, list):
        raise ValueError("settings.json field 'hooks.Stop' must be an array")

    filtered = [group for group in stop if not is_herdrelay_group(group)]
    if install:
        filtered.append(hook_entry(hook_script))
    changed = filtered != stop
    if changed:
        if filtered:
            hooks["Stop"] = filtered
        else:
            hooks.pop("Stop", None)
            if not hooks:
                settings.pop("hooks", None)
    return changed


def write_settings(path: Path, settings: dict[str, object]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.herdrelay-backup-{stamp}")
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    args = parser.parse_args()

    settings_path = args.settings.expanduser().resolve()
    if settings_path.exists():
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("settings.json must contain an object")
        settings = loaded
    else:
        settings = {}
    hook_script = Path(__file__).with_name("agent_stop_hook.py").resolve()
    changed = update_settings(
        settings, install=args.action == "install", hook_script=hook_script
    )
    if not changed:
        print(f"Claude hook already {args.action}ed: {settings_path}")
        return 0
    backup = write_settings(settings_path, settings)
    print(f"Claude hook {args.action}ed: {settings_path}")
    if backup:
        print(f"Backup: {backup}")
    if args.action == "install":
        print("Restart existing Claude Code sessions to load the hook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
