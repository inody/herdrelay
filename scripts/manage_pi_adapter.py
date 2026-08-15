#!/usr/bin/env python3
"""Install or remove HerdRelay's global Pi output extension."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil

MARKER = "HERDRELAY_ADAPTER_ID=pi-output-v1"
DEFAULT_TARGET = Path("~/.pi/agent/extensions/herdrelay-output.ts").expanduser()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()

    source = Path(__file__).parents[1] / "integrations" / "pi-herdrelay-output.ts"
    target = args.target.expanduser().resolve()
    if args.action == "uninstall":
        if not target.exists():
            print(f"Pi adapter already uninstalled: {target}")
            return 0
        if MARKER not in target.read_text(encoding="utf-8"):
            raise ValueError(f"refusing to remove unmanaged file: {target}")
        target.unlink()
        print(f"Pi adapter uninstalled: {target}")
        print("Run /reload in existing Pi sessions.")
        return 0

    content = source.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == content:
        print(f"Pi adapter already installed: {target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.herdrelay-backup-{stamp}")
        shutil.copy2(target, backup)
        print(f"Backup: {backup}")
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    print(f"Pi adapter installed: {target}")
    print("Run /reload in existing Pi sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
