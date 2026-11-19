from __future__ import annotations

from ..config import AppConfig
from ..domain import BackendKind
from ..orca_client import OrcaClient
from ..runtime_client import build_runtime_client
from ..store import Store
from .base import BackendProvider, BackendService
from .compat import CompatibilityBackend
from .orca_output import OrcaOutputSource


def build_backend_provider(
    config: AppConfig, *, store: Store | None = None
) -> BackendProvider:
    try:
        kind = BackendKind(config.runtime.provider)
    except ValueError as exc:
        raise ValueError(
            f"unsupported runtime provider: {config.runtime.provider!r}"
        ) from exc
    client = build_runtime_client(config)
    backend = CompatibilityBackend(kind=kind, client=client)
    services: tuple[BackendService, ...] = ()
    if kind is BackendKind.ORCA and config.enable_streaming:
        if store is None:
            raise ValueError("Orca response streaming requires a Store")
        if not isinstance(client, OrcaClient):
            raise TypeError("Orca runtime requires OrcaClient")
        services = (OrcaOutputSource(config=config, store=store, client=client),)
    return BackendProvider(
        kind=kind,
        backend=backend,
        client=client,
        services=services,
    )
