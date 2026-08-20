"""Hybrid retrieval: Postgres full-text search + vector search, fused with RRF.

Two independent ranked lists over the same 260-chunk corpus, combined by rank
(not score) because `ts_rank_cd` and Chroma cosine distance are not on
comparable scales — see `_rrf_fuse()`. Every returned hit carries the
provenance Step 03's Verifier and the API Contract's citation payload need:
`document_id`, `page`, `char_start`, `char_end`, plus the per-arm ranks/scores
that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.services.embeddings import Embedder
from app.services.ingestion import collection_name_for
from app.services.vector_store import VectorStore

# Tuned against the golden set, not the SIGIR-2009/Elasticsearch default of 60.
# A k=1..100 sweep (offline, against cached query vectors — see
# app/services/evaluation.py) showed no k beats vector-only retrieval on both
# recall@5 and MRR simultaneously with this project's lexical arm: the OR-
# relaxed websearch_to_tsquery (see lexical_search()) is noisy enough that its
# matches occasionally outrank a chunk vector search already had correct at
# rank 1, and low k doesn't fully suppress that. k=5 is the sweet spot found:
# recall@5 0.727 -> 0.818 (beats vector-only), MRR 0.690 -> 0.633 (a real
# regression, not fixed by any k tested). Recorded as a known limitation in
# 09_Memory/DECISIONS.md — Step 03's reranker is the natural place to recover
# precision-at-1, since RRF alone can't distinguish signal from lexical noise.
# A module constant, not a literal, so it stays cheap to re-tune later.
RRF_K = 5

DEFAULT_TOP_K = 5
DEFAULT_CANDIDATE_K = 30

_LEXICAL_SQL = text(
    """
    WITH tq AS (
        SELECT CAST(
            replace(websearch_to_tsquery('english', :q)::text, ' & ', ' | ')
            AS tsquery
        ) AS q
    )
    SELECT c.id, c.document_id, c.page, c.char_start, c.char_end, c.text,
           ts_rank_cd(c.content_tsv, tq.q) AS score
    FROM chunks c, tq
    WHERE c.content_tsv @@ tq.q
      AND (
        CAST(:document_ids AS text[]) IS NULL
        OR c.document_id = ANY(CAST(:document_ids AS text[]))
      )
    ORDER BY score DESC, c.id
    LIMIT :candidate_k
    """
)


@dataclass(frozen=True)
class _LexicalHit:
    chunk_id: str
    document_id: str
    page: int
    char_start: int
    char_end: int
    text: str
    score: float


@dataclass(frozen=True)
class _VectorHit:
    chunk_id: str
    document_id: str
    page: int
    char_start: int
    char_end: int
    text: str | None
    distance: float


@dataclass(frozen=True)
class RetrievedChunk:
    """One fused, fully-provenanced retrieval result.

    `rrf_score` is a fusion artifact (~0.01-0.03), not a calibrated relevance
    number — the API Contract's citation `score` (0-1) is filled in Step 03
    from the reranker, which can honestly produce one. `lexical_*`/`vector_*`
    are None when that arm didn't return this chunk in its candidate window.
    """

    chunk_id: str
    document_id: str
    document_name: str
    page: int
    char_start: int
    char_end: int
    text: str
    rrf_score: float
    lexical_rank: int | None
    lexical_score: float | None
    vector_rank: int | None
    vector_distance: float | None


async def lexical_search(
    session: AsyncSession,
    query: str,
    candidate_k: int,
    document_ids: list[str] | None,
) -> list[_LexicalHit]:
    """Postgres FTS over `chunks.content_tsv` (GIN-indexed, migration 0002).

    `websearch_to_tsquery` ANDs every lexeme by default — measured against the
    live corpus, that returns 0 rows for 10/11 answerable golden questions.
    Rewriting the top-level `&` to `|` keeps the parsing (stemming, phrases,
    negation) but relaxes matching to "any lexeme", taking lexical-only
    recall@5 from 1/11 to 6/11.
    """
    result = await session.execute(
        _LEXICAL_SQL,
        {"q": query, "document_ids": document_ids, "candidate_k": candidate_k},
    )
    return [
        _LexicalHit(
            chunk_id=row.id,
            document_id=row.document_id,
            page=row.page,
            char_start=row.char_start,
            char_end=row.char_end,
            text=row.text,
            score=row.score,
        )
        for row in result
    ]


async def vector_search(
    vector_store: VectorStore,
    embedder: Embedder,
    query: str,
    candidate_k: int,
    document_ids: list[str] | None,
    query_vector: list[float] | None = None,
) -> list[_VectorHit]:
    """Chroma ANN search. Embeds as `input_type="query"` — the corpus was
    embedded as `document`; voyage-4-lite is asymmetric and mixing the two
    costs real recall.

    `query_vector`, when given, skips the embedding call entirely — the
    golden-set evaluation harness embeds each question once, caches the
    vector, and passes it back in on every re-run so repeat evaluation runs
    are $0 and offline.
    """
    collection_name = collection_name_for(embedder)
    where = {"document_id": {"$in": document_ids}} if document_ids else None
    if query_vector is None:
        [query_vector] = await embedder.embed([query], input_type="query")
    hits = await vector_store.query(collection_name, query_vector, candidate_k, where=where)
    return [
        _VectorHit(
            chunk_id=h.id,
            document_id=str(h.metadata["document_id"]),
            page=int(h.metadata["page"]),
            char_start=int(h.metadata["char_start"]),
            char_end=int(h.metadata["char_end"]),
            text=h.document,
            distance=h.distance,
        )
        for h in hits
    ]


def _rrf_fuse(
    lexical_hits: list[_LexicalHit],
    vector_hits: list[_VectorHit],
) -> dict[str, tuple[float, int | None, float | None, int | None, float | None]]:
    """Rank-based fusion. Returns chunk_id -> (rrf_score, lex_rank, lex_score,
    vec_rank, vec_distance). A chunk found by both arms merges into one entry
    carrying both ranks — dedupe is by chunk_id only, never by span overlap:
    the chunker's own 300-char overlap between adjacent chunks means a
    span-overlap rule would drop the neighbour chunk that q11-q13 in the
    golden set specifically need retrieved alongside it.
    """
    fused: dict[str, tuple[float, int | None, float | None, int | None, float | None]] = {}

    for rank, lhit in enumerate(lexical_hits, start=1):
        score = 1.0 / (RRF_K + rank)
        fused[lhit.chunk_id] = (score, rank, lhit.score, None, None)

    for rank, vhit in enumerate(vector_hits, start=1):
        score = 1.0 / (RRF_K + rank)
        if vhit.chunk_id in fused:
            prev_score, lex_rank, lex_score, _, _ = fused[vhit.chunk_id]
            fused[vhit.chunk_id] = (prev_score + score, lex_rank, lex_score, rank, vhit.distance)
        else:
            fused[vhit.chunk_id] = (score, None, None, rank, vhit.distance)

    return fused


async def retrieve(
    query: str,
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    top_k: int = DEFAULT_TOP_K,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    document_ids: list[str] | None = None,
    query_vector: list[float] | None = None,
) -> list[RetrievedChunk]:
    """Fused hybrid retrieval. Both arms fetch `candidate_k` so RRF has depth
    to fuse before truncating to `top_k`. `query_vector` passes through to
    `vector_search` — see its docstring.
    """
    lexical_hits = await lexical_search(session, query, candidate_k, document_ids)
    vector_hits = await vector_search(
        vector_store, embedder, query, candidate_k, document_ids, query_vector=query_vector
    )
    fused = _rrf_fuse(lexical_hits, vector_hits)

    by_id: dict[str, _LexicalHit | _VectorHit] = {}
    for lhit in lexical_hits:
        by_id[lhit.chunk_id] = lhit
    for vhit in vector_hits:
        by_id.setdefault(vhit.chunk_id, vhit)

    ranked_ids = sorted(fused, key=lambda cid: fused[cid][0], reverse=True)[:top_k]
    if not ranked_ids:
        return []

    doc_ids = {by_id[cid].document_id for cid in ranked_ids}
    stmt = select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
    rows = (await session.execute(stmt)).all()
    filenames = {row.id: row.filename for row in rows}

    results: list[RetrievedChunk] = []
    for cid in ranked_ids:
        matched_hit = by_id[cid]
        rrf_score, lex_rank, lex_score, vec_rank, vec_distance = fused[cid]
        chunk_text = matched_hit.text
        if chunk_text is None:
            # Vector arm didn't include `documents` in its include list, or the
            # only source for this chunk was Chroma metadata without a body —
            # shouldn't happen given ChromaVectorStore always stores it, but
            # fail loudly rather than emit a hit the Verifier can't score.
            raise RuntimeError(f"chunk {cid} has no text from either retrieval arm")
        results.append(
            RetrievedChunk(
                chunk_id=cid,
                document_id=matched_hit.document_id,
                document_name=filenames.get(matched_hit.document_id, matched_hit.document_id),
                page=matched_hit.page,
                char_start=matched_hit.char_start,
                char_end=matched_hit.char_end,
                text=chunk_text,
                rrf_score=rrf_score,
                lexical_rank=lex_rank,
                lexical_score=lex_score,
                vector_rank=vec_rank,
                vector_distance=vec_distance,
            )
        )
    return results


def _demo() -> None:
    import asyncio

    from app.core.config import get_settings
    from app.db.session import create_engine, create_session_factory
    from app.services.embeddings import FakeEmbedder
    from app.services.vector_store import ChromaVectorStore

    # --- pure RRF math, no I/O ---
    lex = [
        _LexicalHit("chk_a", "doc_1", 1, 0, 10, "a", 0.9),
        _LexicalHit("chk_b", "doc_1", 1, 10, 20, "b", 0.5),
    ]
    vec = [
        _VectorHit("chk_a", "doc_1", 1, 0, 10, "a", 0.2),
        _VectorHit("chk_c", "doc_1", 1, 20, 30, "c", 0.1),
        _VectorHit("chk_b", "doc_1", 1, 10, 20, "b", 0.3),
    ]
    fused = _rrf_fuse(lex, vec)
    # chk_a: rank 1 in both arms -> highest combined score. chk_b (rank 2
    # lexical, rank 3 vector) still beats chk_c (rank 2 vector-only, absent
    # from lexical) -- appearing in both arms outweighs one strong single-arm
    # rank, which is the whole point of fusing over a single retriever.
    assert fused["chk_a"][0] > fused["chk_b"][0] > fused["chk_c"][0], fused
    assert fused["chk_a"] == (1 / (RRF_K + 1) + 1 / (RRF_K + 1), 1, 0.9, 1, 0.2)
    assert fused["chk_c"] == (1 / (RRF_K + 2), None, None, 2, 0.1)

    # --- deterministic ordering across two identical runs ---
    order1 = sorted(fused, key=lambda cid: fused[cid][0], reverse=True)
    fused2 = _rrf_fuse(lex, vec)
    order2 = sorted(fused2, key=lambda cid: fused2[cid][0], reverse=True)
    assert order1 == order2

    async def run_live() -> None:
        settings = get_settings()
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        embedder = FakeEmbedder(dimensions=1024, model="voyage-4-lite")  # matches the real
        # collection name so we read the real, already-embedded corpus with $0 spend —
        # a fake query vector still exercises Chroma's ANN path and the join/provenance
        # code, it just won't rank meaningfully; that's proven separately by evaluation.py
        # against the cached real query vectors.
        vector_store = ChromaVectorStore(settings.chroma_persist_dir)

        async with session_factory() as session:
            hits = await retrieve(
                "What does the glossary say about Cross-Encoder rerankers?",
                session,
                vector_store,
                embedder,
                top_k=5,
                candidate_k=20,
            )
            assert hits, "expected at least one hit against the real corpus"
            for h in hits:
                assert h.document_id and h.page > 0
                assert h.char_end > h.char_start
                assert h.lexical_rank is not None or h.vector_rank is not None

            # Offset round-trip: text must be an exact slice of the stored chunk row.
            row = (
                await session.execute(
                    text("SELECT text FROM chunks WHERE id = :id"), {"id": hits[0].chunk_id}
                )
            ).scalar_one()
            assert row == hits[0].text

        vector_store.close()
        await engine.dispose()

    asyncio.run(run_live())
    print("ok: RRF math, dedupe-by-id, determinism, and a live hybrid retrieve() all verified")


if __name__ == "__main__":
    _demo()
