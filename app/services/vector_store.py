"""Vector storage behind a narrow interface so the backend stays swappable.

Chroma's persistent client is embedded and fully synchronous. Every call must
therefore cross into a worker thread, or it blocks the event loop for the whole
process — see `check()`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

# Chroma's own parameter types (numpy-or-Sequence unions, invariant dict/Mapping
# types) are stricter than the plain `list[float]` / `dict[str, Any]` this
# module's Protocol exposes to the rest of the app. The `cast()` calls below
# are the adapter boundary — the runtime values genuinely satisfy Chroma's
# contract, mypy's structural check on the union is just overly narrow.

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VectorHit:
    id: str
    distance: float
    metadata: dict[str, Any]
    document: str | None


class VectorStore(Protocol):
    """The contract the rest of the app codes against."""

    async def check(self, timeout: float) -> None:
        """Raise if the store cannot be reached inside `timeout` seconds."""
        ...

    def close(self) -> None: ...

    async def upsert(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None: ...

    async def query(
        self,
        collection_name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    async def delete_by_document(self, collection_name: str, document_id: str) -> int:
        """Delete every vector tagged with this `document_id`. Returns the count deleted."""
        ...

    async def count(self, collection_name: str) -> int: ...


class ChromaVectorStore:
    """Persistent, on-disk Chroma. Connects lazily so a dead store can recover."""

    def __init__(self, persist_dir: Path) -> None:
        self._persist_dir = persist_dir
        self._client: ClientAPI | None = None

    def _get_client(self) -> ClientAPI:
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                # Chroma defaults anonymized_telemetry to True; this project is
                # local-first and sends nothing outward.
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    def _get_collection(self, name: str) -> Collection:
        return self._get_client().get_or_create_collection(name)

    def _ping(self) -> int:
        """Synchronous — only ever call this inside a worker thread."""
        return self._get_client().heartbeat()

    async def check(self, timeout: float) -> None:
        # heartbeat() blocks. Awaiting it directly under asyncio.wait_for would
        # not time out at all — wait_for cannot interrupt a synchronous call, so
        # the event loop would stall until the filesystem answered.
        # ponytail: to_thread is not cancellable, so on timeout the worker thread
        # keeps running to completion. Fine for a probe; revisit if these pile up.
        await asyncio.wait_for(asyncio.to_thread(self._ping), timeout)

    def close(self) -> None:
        self._client = None

    def _upsert_sync(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        self._get_collection(collection_name).upsert(
            ids=ids,
            embeddings=cast(Any, vectors),
            metadatas=cast(Any, metadatas),
            documents=documents,
        )

    async def upsert(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        await asyncio.to_thread(
            self._upsert_sync, collection_name, ids, vectors, metadatas, documents
        )

    def _query_sync(
        self,
        collection_name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None,
    ) -> list[VectorHit]:
        result = self._get_collection(collection_name).query(
            query_embeddings=cast(Any, [vector]),
            n_results=top_k,
            where=cast(Any, where),
            include=["documents", "metadatas", "distances"],
        )
        ids = result["ids"][0]
        distances = result["distances"][0] if result["distances"] else [0.0] * len(ids)
        metadatas = result["metadatas"][0] if result["metadatas"] else [{}] * len(ids)
        documents = result["documents"][0] if result["documents"] else [None] * len(ids)
        return [
            VectorHit(id=i, distance=d, metadata=dict(m or {}), document=doc)
            for i, d, m, doc in zip(ids, distances, metadatas, documents, strict=True)
        ]

    async def query(
        self,
        collection_name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        return await asyncio.to_thread(self._query_sync, collection_name, vector, top_k, where)

    def _delete_by_document_sync(self, collection_name: str, document_id: str) -> int:
        collection = self._get_collection(collection_name)
        where = {"document_id": document_id}
        matched = collection.get(where=cast(Any, where), include=[])
        count = len(matched["ids"])
        if count:
            collection.delete(where=cast(Any, where))
        return count

    async def delete_by_document(self, collection_name: str, document_id: str) -> int:
        return await asyncio.to_thread(self._delete_by_document_sync, collection_name, document_id)

    def _count_sync(self, collection_name: str) -> int:
        return self._get_collection(collection_name).count()

    async def count(self, collection_name: str) -> int:
        return await asyncio.to_thread(self._count_sync, collection_name)
