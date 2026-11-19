from __future__ import annotations

from .config import AppConfig
from .hc_client import HcClient
from .herdr_client import HerdrClient
from .orca_client import OrcaClient
from .runtime import RuntimeClient


def build_runtime_client(config: AppConfig) -> RuntimeClient:
    if config.runtime.provider == "herdr-core":
        return HcClient(config)
    if config.runtime.provider == "orca":
        return OrcaClient(config)
    return HerdrClient(config)
