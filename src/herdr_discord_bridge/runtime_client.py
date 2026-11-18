from __future__ import annotations

from .config import AppConfig
from .hc_client import HcClient
from .herdr_client import HerdrClient


def build_runtime_client(config: AppConfig) -> HerdrClient | HcClient:
    if config.runtime.provider == "herdr-core":
        return HcClient(config)
    return HerdrClient(config)
