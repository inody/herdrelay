#!/usr/bin/env python3
"""One-shot OSC 7501 agent fixture for an isolated Herdr Core smoke test."""

from __future__ import annotations

import base64
import select
import sys
import time


def report(state: str, message: str) -> None:
    title = base64.b64encode(b"HerdRelay isolated smoke").decode("ascii")
    encoded_message = base64.b64encode(message.encode()).decode("ascii")
    sys.stdout.write(
        f"\x1b]7501;state={state}:app=pi:title={title}:msg={encoded_message}\x1b\\"
    )
    sys.stdout.flush()


def main() -> int:
    if sys.argv[1:] == ["--working"]:
        report("working", "waiting for interrupt")
        time.sleep(15)
        return 0
    report("idle", "ready")
    ready, _, _ = select.select([sys.stdin], [], [], 15)
    if not ready:
        return 2
    line = sys.stdin.readline().rstrip("\n")
    report("working", "received")
    print(f"ECHO:{line}", flush=True)
    report("done", "complete")
    time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
