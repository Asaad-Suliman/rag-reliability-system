"""Ingestion state machine: `queued -> parsing -> indexing -> ready | failed | quarantined`.

Runs synchronously end to end — no background worker yet, Step 04 adds one —
but persists every transition, so moving it off the request path later
touches no logic here.

Cost discipline: every chunk in a document is embedded before anything is
written to Postgres or Chroma, so a document is never visible half-indexed.
The tradeoff: if the embedder fails partway through, nothing is checkpointed,
so retrying re-embeds the whole document — including whatever portion already
succeeded and was already billed for. At this project's token volumes (cents
per full corpus) that tradeoff is the right one; per-batch checkpointing would
be tuning a cost that doesn't matter yet.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import AuditEvent, Chunk, Document
from app.services.chunking import chunk_document
from app.services.embeddings import Embedder, EmbeddingError
from app.services.parsing import parse_file
from app.services.parsing.base import ParseError, ParseErrorKind
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Pre-flight guard, checked before any embedding call — refuses rather than
# silently spending on a document nobody sized for.
EMBEDDING_MAX_TOKENS_PER_DOC = 500_000


def collection_name_for(embedder: Embedder) -> str:
    """One Chroma collection per (model, dimensions) pair, so switching
    embedding models can never silently mix incompatible vector spaces.
    """
    slug = embedder.model.replace("-", "").replace(".", "")
    return f"chunks_{slug}_{embedder.dimensions}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audit(session: AsyncSession, user_id: str, document_id: str, event_type: str) -> None:
    session.add(
        AuditEvent(
            user_id=user_id,
            event_type=event_type,
            resource_type="document",
            resource_id=document_id,
        )
    )


async def ingest_document(
    path: Path,
    filename: str,
    user_id: str,
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
) -> Document:
    """Full state machine for one file.

    Idempotent: re-ingesting an already-`ready` document (same user, same
    content hash) is a no-op that spends nothing. Re-ingesting a
    `failed`/`quarantined`/interrupted one retries in place, from `queued`.
    """
    sha256 = _sha256_file(path)
    size_bytes = path.stat().st_size

    existing = (
        await session.execute(
            select(Document).where(Document.user_id == user_id, Document.sha256 == sha256)
        )
    ).scalar_one_or_none()

    if existing is not None and existing.status == "ready":
        logger.info("ingest: dedupe hit, already ready", extra={"document_id": existing.id})
        return existing

    if existing is not None:
        document = existing
        document.status = "queued"
        document.error = None
    else:
        document = Document(
            user_id=user_id,
            filename=filename,
            mime_type="application/octet-stream",  # overwritten once sniffed, below
            size_bytes=size_bytes,
            sha256=sha256,
            status="queued",
            chunk_count=0,
        )
        session.add(document)
        await session.flush()  # assigns document.id before anything references it

    _audit(session, user_id, document.id, "ingest_queued")
    await session.commit()

    document.status = "parsing"
    await session.commit()
    try:
        mime_type, parsed = parse_file(path)
    except ParseError as exc:
        document.status = "quarantined" if exc.kind is ParseErrorKind.UNSAFE_STRUCTURE else "failed"
        document.error = exc.message
        _audit(session, user_id, document.id, "ingest_parse_failed")
        await session.commit()
        return document

    document.mime_type = mime_type
    document.page_count = len(parsed.pages)
    document.status = "indexing"
    await session.commit()

    chunked = chunk_document(parsed)
    if not chunked.chunks:
        document.status = "failed"
        document.error = "No extractable content was found to index."
        _audit(session, user_id, document.id, "ingest_no_chunks")
        await session.commit()
        return document

    total_tokens = sum(c.token_estimate for c in chunked.chunks)
    if total_tokens > EMBEDDING_MAX_TOKENS_PER_DOC:
        document.status = "failed"
        document.error = "Document exceeds the indexing size limit."
        _audit(session, user_id, document.id, "ingest_over_token_limit")
        await session.commit()
        return document

    collection_name = collection_name_for(embedder)
    # A no-op for a fresh document (nothing was ever written for it), but
    # protects any future caller that resets a 'ready' document back to
    # 'queued' to force a reindex — old chunks/vectors must not survive that.
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await vector_store.delete_by_document(collection_name, document.id)

    texts = [c.text for c in chunked.chunks]
    try:
        vectors = await embedder.embed(texts, input_type="document")
    except EmbeddingError as exc:
        document.status = "failed"
        document.error = "Indexing failed because the embedding service was unavailable."
        _audit(session, user_id, document.id, "ingest_embedding_failed")
        await session.commit()
        logger.warning("embedding failed", extra={"document_id": document.id, "reason": str(exc)})
        return document

    chunk_rows = [
        Chunk(
            id=new_id("chk"),
            document_id=document.id,
            ordinal=span.ordinal,
            page=span.page,
            char_start=span.char_start,
            char_end=span.char_end,
            text=span.text,
            token_estimate=span.token_estimate,
        )
        for span in chunked.chunks
    ]
    for row in chunk_rows:
        row.vector_id = row.id
    session.add_all(chunk_rows)

    await vector_store.upsert(
        collection_name,
        ids=[c.id for c in chunk_rows],
        vectors=vectors,
        metadatas=[
            {
                "document_id": document.id,
                "page": c.page,
                "char_start": c.char_start,
                "char_end": c.char_end,
            }
            for c in chunk_rows
        ],
        documents=[c.text for c in chunk_rows],
    )

    document.status = "ready"
    document.chunk_count = len(chunk_rows)
    document.indexed_at = datetime.now(UTC)
    _audit(session, user_id, document.id, "ingest_ready")
    await session.commit()
    return document


async def delete_document(
    document: Document, session: AsyncSession, vector_store: VectorStore, embedder: Embedder
) -> int:
    """Delete the document row (chunks CASCADE at the DB level) and its
    vectors from the vector store. Returns the number of vectors deleted.
    """
    collection_name = collection_name_for(embedder)
    deleted_vectors = await vector_store.delete_by_document(collection_name, document.id)
    await session.delete(document)
    await session.commit()
    return deleted_vectors


def _demo() -> None:
    import asyncio
    import tempfile

    from app.core.config import get_settings
    from app.db.models import User
    from app.db.session import create_engine, create_session_factory
    from app.services.embeddings import FakeEmbedder
    from app.services.vector_store import ChromaVectorStore

    corpus = Path("/home/asaad/Downloads/Mastering RAG 2026_compressed.pdf")
    assert corpus.exists(), f"self-check requires the corpus fixture at {corpus}"

    async def run() -> None:
        settings = get_settings()
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        embedder = FakeEmbedder(dimensions=8)  # small: this checks the pipeline, not vector quality

        with tempfile.TemporaryDirectory() as chroma_dir:
            vector_store = ChromaVectorStore(Path(chroma_dir))
            collection_name = collection_name_for(embedder)

            async with session_factory() as session:
                user = User(email="ingestion-selfcheck@test.local", password_hash="x")
                session.add(user)
                await session.flush()
                user_id = user.id

                try:
                    doc = await ingest_document(
                        corpus, corpus.name, user_id, session, vector_store, embedder
                    )
                    assert doc.status == "ready", doc.error
                    assert doc.chunk_count > 0
                    assert doc.page_count == 247

                    rows = (
                        (await session.execute(select(Chunk).where(Chunk.document_id == doc.id)))
                        .scalars()
                        .all()
                    )
                    assert len(rows) == doc.chunk_count
                    assert all(r.vector_id == r.id for r in rows)

                    vector_count = await vector_store.count(collection_name)
                    assert vector_count == doc.chunk_count, (vector_count, doc.chunk_count)

                    # Re-ingest the same bytes: dedupe, same document, no new rows.
                    doc2 = await ingest_document(
                        corpus, corpus.name, user_id, session, vector_store, embedder
                    )
                    assert doc2.id == doc.id
                    assert doc2.status == "ready"

                    deleted = await delete_document(doc, session, vector_store, embedder)
                    assert deleted == doc.chunk_count
                    remaining = (
                        (await session.execute(select(Chunk).where(Chunk.document_id == doc.id)))
                        .scalars()
                        .all()
                    )
                    assert remaining == []
                    assert await vector_store.count(collection_name) == 0
                finally:
                    await session.execute(delete(User).where(User.id == user_id))
                    await session.commit()

        await engine.dispose()
        print(
            f"ok: {doc.chunk_count} chunks — full ingest, dedupe, and delete "
            "all verified end to end with FakeEmbedder"
        )

    asyncio.run(run())


if __name__ == "__main__":
    _demo()
