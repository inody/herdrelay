import os

from herdr_discord_bridge.config import load_config


def test_load_config_reads_dashboard_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
enable_dashboard: true
dashboard:
  refresh_seconds: 12
"""
    )

    config = load_config(config_path)

    assert config.enable_dashboard is True
    assert config.dashboard.refresh_seconds == 12
