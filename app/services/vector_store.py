"""Vector storage behind a narrow interface so the backend stays swappable.

Chroma's persistent client is embedded and fully synchronous. Every call must
therefore cross into a worker thread, or it blocks the event loop for the whole
process — see `check()`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

import chromadb
from chromadb.api import ClientAPI
from chromadb.config import Settings as ChromaSettings

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    """The contract the rest of the app codes against. Step 02 adds add/query."""

    async def check(self, timeout: float) -> None:
        """Raise if the store cannot be reached inside `timeout` seconds."""
        ...

    def close(self) -> None: ...


class ChromaVectorStore:
    """Persistent, on-disk Chroma. Connects lazily so a dead store can recover."""

    def __init__(self, persist_dir: Path) -> None:
        self._persist_dir = persist_dir
        self._client: ClientAPI | None = None

    def _ping(self) -> int:
        """Synchronous — only ever call this inside a worker thread."""
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                # Chroma defaults anonymized_telemetry to True; this project is
                # local-first and sends nothing outward.
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client.heartbeat()

    async def check(self, timeout: float) -> None:
        # heartbeat() blocks. Awaiting it directly under asyncio.wait_for would
        # not time out at all — wait_for cannot interrupt a synchronous call, so
        # the event loop would stall until the filesystem answered.
        # ponytail: to_thread is not cancellable, so on timeout the worker thread
        # keeps running to completion. Fine for a probe; revisit if these pile up.
        await asyncio.wait_for(asyncio.to_thread(self._ping), timeout)

    def close(self) -> None:
        self._client = None
