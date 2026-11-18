from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import tempfile

from .config import AppConfig, load_config
from .hc_client import HcClient


def _allowlist_path(config: AppConfig) -> Path:
    value = config.runtime.core.allowed_terminal_addresses_file
    if value is None:
        raise ValueError(
            "runtime.herdr_core.allowed_terminal_addresses_file is required "
            "for herdrelay-targets"
        )
    return Path(value)


def _file_targets(config: AppConfig) -> set[str]:
    path = _allowlist_path(config)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }


def _write_targets(path: Path, targets: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = "".join(f"{target}\n" for target in sorted(targets))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _client_with_target(config: AppConfig, address: str) -> HcClient:
    core = replace(
        config.runtime.core,
        allowed_terminal_addresses=(
            config.runtime.core.allowed_terminal_addresses | frozenset({address})
        ),
    )
    return HcClient(replace(config, runtime=replace(config.runtime, core=core)))


def allow_target(config: AppConfig, address: str) -> bool:
    address = address.strip()
    if not address or any(character.isspace() for character in address):
        raise ValueError("target address must be a nonempty value without whitespace")
    if config.runtime.provider != "herdr-core":
        raise ValueError("herdrelay-targets requires runtime.provider=herdr-core")
    targets = _file_targets(config)
    if address in targets or address in config.runtime.core.allowed_terminal_addresses:
        return False
    client = _client_with_target(config, address)
    live = next(
        (target for target in client.list_targets() if target.target == address), None
    )
    if live is None or not live.agent_name:
        raise ValueError(
            f"{address} is not an eligible live agent in the configured Core"
        )
    targets.add(address)
    _write_targets(_allowlist_path(config), targets)
    return True


def remove_target(config: AppConfig, address: str) -> bool:
    targets = _file_targets(config)
    if address not in targets:
        return False
    targets.remove(address)
    _write_targets(_allowlist_path(config), targets)
    return True


def list_targets(config: AppConfig) -> list[tuple[str, str]]:
    if config.runtime.provider != "herdr-core":
        raise ValueError("herdrelay-targets requires runtime.provider=herdr-core")
    configured = sorted(config.runtime.core.allowed_terminal_addresses)
    client = HcClient(config)
    live = {target.target: target for target in client.list_targets()}
    return [
        (
            address,
            (
                f"live {live[address].workspace_label or '-'}"
                f"/{live[address].agent_name or '?'}"
                if address in live
                else "not-live"
            ),
        )
        for address in configured
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage exact Herdr Core target addresses for HerdRelay"
    )
    parser.add_argument("--config", default="config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list configured targets and live state")
    allow_parser = subparsers.add_parser("allow", help="allow one live Core agent")
    allow_parser.add_argument("address")
    remove_parser = subparsers.add_parser("remove", help="remove one file-managed target")
    remove_parser.add_argument("address")
    args = parser.parse_args()

    config = load_config(args.config, require_discord_token=False)
    changed = False
    try:
        if args.command == "list":
            for address, state in list_targets(config):
                print(f"{address}\t{state}")
        elif args.command == "allow":
            changed = allow_target(config, args.address)
            print("added" if changed else "already-allowed", args.address)
        else:
            changed = remove_target(config, args.address)
            print("removed" if changed else "not-file-managed", args.address)
    except ValueError as exc:
        parser.error(str(exc))
    if changed:
        print("Restart HerdRelay to apply the updated allowlist.")


if __name__ == "__main__":
    main()
