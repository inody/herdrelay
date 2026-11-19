from __future__ import annotations

from ..config import AppConfig
from ..domain import BackendKind
from ..runtime_client import build_runtime_client
from .base import BackendProvider
from .compat import CompatibilityBackend


def build_backend_provider(config: AppConfig) -> BackendProvider:
    try:
        kind = BackendKind(config.runtime.provider)
    except ValueError as exc:
        raise ValueError(
            f"unsupported runtime provider: {config.runtime.provider!r}"
        ) from exc
    if kind is BackendKind.ORCA:
        raise ValueError("Orca backend is not implemented yet")

    client = build_runtime_client(config)
    backend = CompatibilityBackend(kind=kind, client=client)
    return BackendProvider(kind=kind, backend=backend, client=client)
