from __future__ import annotations

from types import SimpleNamespace

import pytest

from herdrelay.config import AppConfig, HcRuntimeConfig, RuntimeConfig
from herdrelay.models import HerdrTarget
from herdrelay.target_cli import allow_target, remove_target


def config(path) -> AppConfig:
    return AppConfig(
        discord_token="",
        runtime=RuntimeConfig(
            provider="herdr-core",
            core=HcRuntimeConfig(
                allowed_terminal_addresses_file=str(path),
                allowed_terminal_addresses=frozenset(),
            ),
        ),
    )


def test_allow_and_remove_target_use_private_atomic_file(tmp_path, monkeypatch):
    path = tmp_path / "targets.txt"
    app_config = config(path)
    client = SimpleNamespace(
        list_targets=lambda: [
            HerdrTarget(
                target="t12",
                agent_name="pi",
                workspace_label="project",
            )
        ]
    )
    monkeypatch.setattr(
        "herdrelay.target_cli._client_with_target",
        lambda supplied, address: client,
    )

    assert allow_target(app_config, "t12") is True
    assert allow_target(app_config, "t12") is False
    assert path.read_text() == "t12\n"
    assert path.stat().st_mode & 0o777 == 0o600

    assert remove_target(app_config, "t12") is True
    assert remove_target(app_config, "t12") is False
    assert path.read_text() == ""


def test_allow_target_requires_an_eligible_live_agent(tmp_path, monkeypatch):
    app_config = config(tmp_path / "targets.txt")
    monkeypatch.setattr(
        "herdrelay.target_cli._client_with_target",
        lambda supplied, address: SimpleNamespace(list_targets=lambda: []),
    )

    with pytest.raises(ValueError, match="eligible live agent"):
        allow_target(app_config, "t99")


def test_target_cli_requires_file_managed_allowlist():
    app_config = AppConfig(
        discord_token="",
        runtime=RuntimeConfig(provider="herdr-core"),
    )

    with pytest.raises(ValueError, match="allowed_terminal_addresses_file"):
        remove_target(app_config, "t1")
