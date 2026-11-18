#!/usr/bin/env python3
"""Check the installed hc CLI schema without contacting or starting a Core."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Sequence

from herdr_discord_bridge.hc_protocol import (
    HcProtocolError,
    inspect_openrpc_schema,
)


def run_command(argv: Sequence[str], *, timeout: float) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out: {' '.join(argv)}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(argv)}"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout


def probe(hc_path: str, *, timeout: float = 10) -> dict[str, object]:
    version_output = run_command([hc_path, "--version"], timeout=timeout).strip()
    schema_output = run_command([hc_path, "schema"], timeout=timeout)
    try:
        schema = json.loads(schema_output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"hc schema returned invalid JSON: {exc}") from exc
    report = inspect_openrpc_schema(schema, cli_version=version_output or None)
    return report.as_dict()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check whether the installed hc CLI schema contains the methods "
            "and parameters planned for HerdRelay. This static check does not "
            "contact or start Herdr Core."
        )
    )
    parser.add_argument("--hc", default="hc", help="Path to the hc executable")
    parser.add_argument("--timeout", type=float, default=10, help="Command timeout")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args(argv)

    try:
        result = probe(args.hc, timeout=args.timeout)
    except (RuntimeError, HcProtocolError) as exc:
        if args.json:
            print(json.dumps({"compatible": False, "error": str(exc)}))
        else:
            print(f"hc compatibility check failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "compatible" if result["compatible"] else "incompatible"
        print(f"hc schema: {status}")
        print(f"CLI: {result['cli_version'] or 'unknown'}")
        print(f"Schema: {result['schema_version'] or 'unknown'}")
        print(
            "Expected protocol generation: "
            f"{result['expected_protocol_generation']} (live handshake not performed)"
        )
        if result["missing_methods"]:
            print("Missing methods: " + ", ".join(result["missing_methods"]))
        if result["missing_params"]:
            for method, params in result["missing_params"].items():
                print(f"Missing parameters for {method}: {', '.join(params)}")
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
