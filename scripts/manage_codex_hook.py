#!/usr/bin/env python3
"""Install or remove HerdRelay's Codex Stop hook without changing notify."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import tomllib

BEGIN = "# BEGIN HERDRELAY CODEX OUTPUT HOOK"
END = "# END HERDRELAY CODEX OUTPUT HOOK"
MARKER = "herdrelay-codex-stop-hook"
DEFAULT_CONFIG = Path("~/.codex/config.toml").expanduser()


def hook_assignment(hook_script: Path) -> str:
    command = (
        f"/usr/bin/env python3 {shlex.quote(str(hook_script))} --agent codex"
        f" # {MARKER}"
    )
    return (
        "Stop = [{ matcher = \"*\", hooks = [{ type = \"command\", command = "
        f"{json.dumps(command)}, timeout = 5 }}] }}]"
    )


def install_text(text: str, hook_script: Path) -> str:
    if BEGIN in text or MARKER in text:
        return text
    parsed = tomllib.loads(text) if text.strip() else {}
    hooks = parsed.get("hooks")
    block_line = hook_assignment(hook_script)
    if hooks is None:
        separator = "" if not text or text.endswith("\n\n") else "\n"
        return f"{text}{separator}{BEGIN}\n[hooks]\n{block_line}\n{END}\n"
    if not isinstance(hooks, dict):
        raise ValueError("config.toml field 'hooks' must be a table")
    if "Stop" in hooks:
        raise ValueError("existing hooks.Stop must be merged manually")
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == "[hooks]":
            lines.insert(index + 1, f"{BEGIN}\n{block_line}\n{END}\n")
            return "".join(lines)
    raise ValueError("could not locate existing [hooks] table")


def uninstall_text(text: str) -> str:
    if BEGIN not in text:
        return text
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    if end < len(text) and text[end] == "\n":
        end += 1
    block = text[start:end]
    owns_table = "[hooks]" in block
    result = text[:start] + text[end:]
    if owns_table:
        result = result.rstrip() + "\n"
    return result


def write_config(path: Path, text: str) -> Path | None:
    tomllib.loads(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.herdrelay-backup-{stamp}")
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    temporary.replace(path)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    path = args.config.expanduser().resolve()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    hook_script = Path(__file__).with_name("agent_stop_hook.py").resolve()
    updated = (
        install_text(original, hook_script)
        if args.action == "install"
        else uninstall_text(original)
    )
    if updated == original:
        print(f"Codex hook already {args.action}ed: {path}")
        return 0
    backup = write_config(path, updated)
    print(f"Codex hook {args.action}ed: {path}")
    if backup:
        print(f"Backup: {backup}")
    print("Restart existing Codex sessions to reload hooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
