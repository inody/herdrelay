#!/usr/bin/env python3
"""Copy-truncate HerdRelay launchd logs and retain compressed backups."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import shutil
import sys

MAX_BYTES = int(os.environ.get("HERDRELAY_LOG_MAX_BYTES", 10 * 1024 * 1024))
BACKUP_COUNT = int(os.environ.get("HERDRELAY_LOG_BACKUP_COUNT", 3))


def rotate(path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUP_COUNT) -> bool:
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except FileNotFoundError:
        return False

    if backups <= 0:
        path.write_bytes(b"")
        return True

    oldest = path.with_name(f"{path.name}.{backups}.gz")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}.gz")
        destination = path.with_name(f"{path.name}.{index + 1}.gz")
        if source.exists():
            source.replace(destination)

    backup = path.with_name(f"{path.name}.1.gz")
    temporary = backup.with_suffix(backup.suffix + ".tmp")
    with path.open("rb") as source, gzip.open(temporary, "wb") as destination:
        shutil.copyfileobj(source, destination)
    temporary.replace(backup)

    # launchd keeps the active file descriptor open. Truncating the same inode
    # lets the running process continue writing to the configured path.
    with path.open("r+b") as active:
        active.truncate(0)
    return True


def main(argv: list[str]) -> int:
    for raw_path in argv[1:]:
        rotate(Path(raw_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
