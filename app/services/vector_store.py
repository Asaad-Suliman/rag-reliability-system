"""Vector storage behind a narrow interface so the backend stays swappable.

The interface earned its keep: the Chroma/HNSW backend it was written against
was removed in favour of `ExactVectorStore` and nothing outside this file
changed. See that class for why.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

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

    async def count_by_document(self, collection_name: str, document_id: str) -> int:
        """Number of vectors tagged with this `document_id`. Read-only — lets a
        caller (the CLI's `stats`) compare against Postgres `chunk_count`
        without deleting anything, to detect an interrupted delete/reindex
        that left the two stores disagreeing.
        """
        ...


class InMemoryVectorStore:
    """Writable, ephemeral, exhaustive. The ingestion pipeline's test double.

    `ingestion._demo()` needs a store it can actually write to, to prove the
    upsert/delete/reindex paths still work end to end. It used to build a
    throwaway Chroma index in a tempdir for that — 40 lines here removes the
    project's dependency on an ANN engine it no longer uses for anything else.

    Never wired into the app: `ExactVectorStore` serves every real read.
    """

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, VectorHit]] = {}

    async def check(self, timeout: float) -> None:
        del timeout

    def close(self) -> None:
        self._rows.clear()

    async def upsert(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        collection = self._rows.setdefault(collection_name, {})
        for chunk_id, vector, metadata, document in zip(
            ids, vectors, metadatas, documents, strict=True
        ):
            collection[chunk_id] = VectorHit(
                id=chunk_id,
                distance=0.0,
                metadata=dict(metadata) | {"_vector": list(vector)},
                document=document,
            )

    async def query(
        self,
        collection_name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        query_vector = np.asarray(vector, dtype=np.float32)
        hits: list[VectorHit] = []
        for hit in self._rows.get(collection_name, {}).values():
            metadata = {k: v for k, v in hit.metadata.items() if k != "_vector"}
            if where is not None and not _matches(metadata, where):
                continue
            stored = np.asarray(hit.metadata["_vector"], dtype=np.float32)
            distance = float(((stored - query_vector) ** 2).sum())
            hits.append(replace(hit, distance=distance, metadata=metadata))
        # Same total order as ExactVectorStore: distance, then chunk id.
        return sorted(hits, key=lambda h: (h.distance, h.id))[:top_k]

    async def delete_by_document(self, collection_name: str, document_id: str) -> int:
        collection = self._rows.get(collection_name, {})
        doomed = [k for k, h in collection.items() if h.metadata.get("document_id") == document_id]
        for chunk_id in doomed:
            del collection[chunk_id]
        return len(doomed)

    async def count(self, collection_name: str) -> int:
        return len(self._rows.get(collection_name, {}))

    async def count_by_document(self, collection_name: str, document_id: str) -> int:
        return sum(
            1
            for h in self._rows.get(collection_name, {}).values()
            if h.metadata.get("document_id") == document_id
        )


def _matches(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
    """The two filter shapes this app emits: `{k: v}` and `{k: {"$in": [...]}}`."""
    for key, condition in where.items():
        if isinstance(condition, dict):
            if metadata.get(key) not in condition["$in"]:
                return False
        elif metadata.get(key) != condition:
            return False
    return True


# --- exact brute-force search ------------------------------------------------
# Replaces the HNSW arm. Not a defect fix: HNSW's measured miss rate at k=40 was
# 0.135% and no miss ever sat above exact-rank 15, so no gate figure moved
# (chunk 7.1g). It is a determinism, simplicity and speed change — HNSW was
# cross-process nondeterministic on 7 of 36 questions, build determinism proved
# unreachable in chromadb 1.5.9, and exact search over 260 vectors is 19x
# faster (0.483 ms vs 9.281 ms mean). ANN earns its place back at N ~= 4,000
# (mean) / 6,700 (p95); the corpus is 260.

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"
VECTORS_FILENAME = "corpus_vectors.npy"
MANIFEST_FILENAME = "corpus_vectors.json"

# The corpus is frozen: 260 vectors, voyage-4-lite, never to be re-embedded.
# A different count means the artifact is not the one every committed figure
# was measured against, so the store refuses to answer rather than answering
# from something else.
EXPECTED_COUNT = 260


class CorpusUnavailableError(RuntimeError):
    """The frozen vector artifact is missing, truncated, or not the pinned one.

    Always fatal, never fallen back from. A vector arm that silently degrades
    to "some other corpus" produces citations that look identical to real ones.
    """


@dataclass(frozen=True)
class _Corpus:
    collection: str
    ids: list[str]
    matrix: np.ndarray
    metadatas: list[dict[str, Any]]
    documents: list[str]


def _load_corpus(corpus_dir: Path) -> _Corpus:
    vectors_path = corpus_dir / VECTORS_FILENAME
    manifest_path = corpus_dir / MANIFEST_FILENAME
    for path in (vectors_path, manifest_path):
        if not path.is_file():
            raise CorpusUnavailableError(
                f"frozen vector corpus missing at {path}. "
                "Regenerate with: uv run --no-sync python -m scripts.export_vectors"
            )

    manifest = json.loads(manifest_path.read_text())
    matrix = np.load(vectors_path, allow_pickle=False)

    digest = hashlib.sha256(matrix.tobytes()).hexdigest()
    if digest != manifest["vectors_sha256"]:
        raise CorpusUnavailableError(
            f"{vectors_path} does not match the digest pinned in {manifest_path}:\n"
            f"  pinned {manifest['vectors_sha256']}\n  actual {digest}"
        )

    records = manifest["records"]
    if not (len(records) == matrix.shape[0] == manifest["count"] == EXPECTED_COUNT):
        raise CorpusUnavailableError(
            f"expected {EXPECTED_COUNT} frozen vectors, found "
            f"matrix={matrix.shape[0]} manifest={manifest['count']} records={len(records)}"
        )
    if matrix.dtype != np.float32 or matrix.shape[1] != manifest["dimensions"]:
        raise CorpusUnavailableError(
            f"expected float32 x {manifest['dimensions']}, found {matrix.dtype} {matrix.shape}"
        )

    return _Corpus(
        collection=manifest["collection"],
        ids=[r["id"] for r in records],
        matrix=np.ascontiguousarray(matrix),
        metadatas=[
            {k: r[k] for k in ("document_id", "page", "char_start", "char_end")} for r in records
        ],
        documents=[r["document"] for r in records],
    )


def _candidate_rows(corpus: _Corpus, where: dict[str, Any] | None) -> np.ndarray:
    """Row indices surviving `where`. Only the one filter shape the app emits.

    `retrieval.vector_search` builds exactly `{"document_id": {"$in": [...]}}`
    and nothing else ever reaches here. Anything else is a wiring change, and
    silently ignoring it would return unfiltered results that look filtered.
    """
    if where is None:
        return np.arange(len(corpus.ids))
    match where:
        case {"document_id": {"$in": list(document_ids)}} if len(where) == 1:
            wanted = set(document_ids)
            return np.array(
                [i for i, m in enumerate(corpus.metadatas) if m["document_id"] in wanted],
                dtype=np.intp,
            )
        case _:
            raise CorpusUnavailableError(f"unsupported metadata filter: {where!r}")


class ExactVectorStore:
    """Exhaustive L2 search over the frozen corpus. Read-only, deterministic."""

    def __init__(self, corpus_dir: Path = CORPUS_DIR) -> None:
        self._corpus_dir = corpus_dir
        self._corpus: _Corpus | None = None

    def _get_corpus(self) -> _Corpus:
        if self._corpus is None:
            self._corpus = _load_corpus(self._corpus_dir)
        return self._corpus

    async def check(self, timeout: float) -> None:
        # No socket, no lock, no WAL replay — the whole store is a 1.02 MiB
        # mmap-able file. `timeout` is part of the Protocol and is honoured
        # only in the sense that this cannot block on anything.
        del timeout
        self._get_corpus()

    def close(self) -> None:
        self._corpus = None

    async def upsert(
        self,
        collection_name: str,
        ids: list[str],
        vectors: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        raise CorpusUnavailableError(
            "the vector corpus is frozen and this store is read-only. Re-embedding is a "
            "deliberate, paid operation: ingest under a writable store, then re-run "
            "scripts.export_vectors."
        )

    async def delete_by_document(self, collection_name: str, document_id: str) -> int:
        raise CorpusUnavailableError(
            f"the vector corpus is frozen and this store is read-only; cannot delete {document_id}."
        )

    async def query(
        self,
        collection_name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        # Not dispatched to a worker thread, unlike Chroma: the whole search is
        # ~0.5 ms of numpy, and asyncio.to_thread costs more than that.
        corpus = self._get_corpus()
        if collection_name != corpus.collection:
            raise CorpusUnavailableError(
                f"collection {collection_name!r} requested but the frozen corpus is "
                f"{corpus.collection!r} — the embedding model or dimensions changed."
            )

        query_vector = np.asarray(vector, dtype=np.float32)
        if query_vector.shape != (corpus.matrix.shape[1],):
            raise CorpusUnavailableError(
                f"query vector is {query_vector.shape}, corpus is {corpus.matrix.shape[1]}-dim"
            )

        # SQUARED L2, matching what the collection was built with
        # (`config_json.vector_index.space == "l2"`) and what hnswlib's L2Space
        # returned as `distance`. Written as a plain ufunc reduction, NOT the
        # ||a||^2 - 2a.b + ||b||^2 identity: that form goes through BLAS gemv,
        # whose accumulation order varies with thread count, which is exactly
        # the cross-process nondeterminism this change exists to remove.
        distances = ((corpus.matrix - query_vector) ** 2).sum(axis=1)

        candidates = _candidate_rows(corpus, where)
        k = min(top_k, candidates.size)
        if k == 0:
            return []
        top = candidates[np.argpartition(distances[candidates], k - 1)[:k]]
        # argpartition leaves the k unordered. Ascending distance, then
        # ascending chunk id — a total order, so the result is identical across
        # processes even if two chunks ever tie (none do on this corpus).
        order = sorted(top, key=lambda i: (float(distances[i]), corpus.ids[i]))

        return [
            VectorHit(
                id=corpus.ids[i],
                distance=float(distances[i]),
                metadata=dict(corpus.metadatas[i]),
                document=corpus.documents[i],
            )
            for i in order
        ]

    async def count(self, collection_name: str) -> int:
        corpus = self._get_corpus()
        return len(corpus.ids) if collection_name == corpus.collection else 0

    async def count_by_document(self, collection_name: str, document_id: str) -> int:
        corpus = self._get_corpus()
        if collection_name != corpus.collection:
            return 0
        return sum(1 for m in corpus.metadatas if m["document_id"] == document_id)
