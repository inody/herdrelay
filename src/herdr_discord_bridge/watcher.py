from __future__ import annotations

import asyncio
import logging

LOG = logging.getLogger(__name__)


class EventWatcher:
    """Placeholder for the raw Herdr socket watcher planned after read-only MVP."""

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(3600)

