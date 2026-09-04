"""Hybrid retrieval: Postgres full-text search + vector search, fused with RRF.

Two independent ranked lists over the same 260-chunk corpus, combined by rank
(not score) because `ts_rank_cd` and vector L2 distance are not on
comparable scales — see `_rrf_fuse()`. Every returned hit carries the
provenance Step 03's Verifier and the API Contract's citation payload need:
`document_id`, `page`, `char_start`, `char_end`, plus the per-arm ranks/scores
that produced it.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.services.embeddings import Embedder
from app.services.ingestion import collection_name_for
from app.services.reranking import (
    NOOP_RERANKER,
    RERANK_N,
    Reranker,
    RerankTiming,
)
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

# The far-field gate's cut, on the vector arm's top-1 L2 distance.
#
# **UNFITTED PLACEHOLDER.** Selection rule, fixed in advance of looking at any
# candidate-cut outcome table: *the maximum observed answerable top-1 distance;
# no observed answerable question is refused.* That rule names one order
# statistic of the negative class, so it has no free parameter and could have
# been written down before any sweep existed.
#
# It was **NOT fitted on the class-1 triage set.** That set is pre-registered
# TRIAGE ONLY and may not be used to fit a threshold; the candidate-cut sweep
# that exists was recorded specifically so its per-cut outcome table could be
# marked unusable for selection. Calibrating this number requires a held-out
# set, and **no such set exists.**
#
# Known cost, recorded at adoption rather than discovered later: 3 of the 12
# class-1 out-of-domain probes fall below this cut and are therefore NOT
# refused — a 25% miss rate on the easiest possible positive class. That is the
# price of the rule's guarantee that no observed answerable question is refused.
#
# Decision, evidence digests and the full derivation: commit `bc1e261`.
FAR_FIELD_ABSTAIN_DISTANCE = 1.4932

# Exactly three states, and deliberately no fourth and no confidence score:
# nothing committed establishes a groundedness signal, so any score this
# returned would be invented.
FarFieldVerdict = Literal["ANSWER", "ABSTAIN_OUT_OF_DOMAIN", "ANSWER_UNVERIFIED"]

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


class FarFieldInputError(RuntimeError):
    """The far-field gate was handed a retrieval it cannot judge.

    Always fatal, never fallen back from, and deliberately not a verdict. A
    far-field gate that answers "out of domain" because it could not find a distance
    is fabricating the refusal it exists to justify, and one that answers
    "unverified" is fabricating the opposite. Both look identical to a real
    decision downstream. The caller must decide what an unjudgeable retrieval
    means; this module refuses to decide it silently.
    """


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

    # Alongside `rrf_score`, never replacing it: `rrf_score` is still live
    # provenance (cli.py prints it, evaluation.py's abstention column *is* the
    # top-1 rrf_score), so overwriting it would break comparability with the
    # chunk 3 baseline. Both None when no reranker ran, which makes "was this
    # reranked?" answerable from the object alone rather than from context.
    # `rerank_score` is the raw cross-encoder logit — see RerankResult.scores.
    rerank_score: float | None = None
    rerank_rank: int | None = None


@dataclass(frozen=True)
class Retrieval:
    """`hits` plus what reranking cost, if it ran.

    A wrapper rather than a bare list because Step 04 has to fill the API
    Contract's `timings_ms.rerank`, and the alternative — recording the last
    call's timing on the reranker itself — is a concurrency bug waiting to
    happen: the reranker is a process-wide singleton, so two in-flight requests
    would overwrite each other's numbers.

    `rerank` is None when the reranker did no work (`NoOpReranker`, or an empty
    candidate set).
    """

    hits: list[RetrievedChunk]
    rerank: RerankTiming | None = None


@dataclass(frozen=True)
class FarFieldDecision:
    """The verdict plus the number that produced it.

    `top1_distance` is carried so the decision is auditable from the object
    alone rather than by re-running retrieval — the same reasoning that puts
    the per-arm ranks on `RetrievedChunk`.
    """

    verdict: FarFieldVerdict
    top1_distance: float


def far_field_gate(retrieval: Retrieval) -> FarFieldDecision:
    """Far-field refusal on the vector arm's top-1 distance. Nothing else.

    `top-1 >= FAR_FIELD_ABSTAIN_DISTANCE` -> **ABSTAIN_OUT_OF_DOMAIN**;
    otherwise **ANSWER_UNVERIFIED**. `>=` is inclusive at the boundary, so the
    question that set the constant is itself refused.

    **ANSWER is defined in `FarFieldVerdict` but is NOT reachable from this
    function, and that is the finding, not an oversight.** Reaching it would
    require a committed groundedness signal, and none exists: the retrieval-side
    measurement line established that its signals separate *out-of-domain*, not
    *unanswerable*, and that in-domain unanswerability is not distance-separable
    at all (commit `bc1e261`). ANSWER stays in the type because a Verifier is
    the component that can license it; returning it from here would be a claim
    this module cannot support. **No in-band abstention is claimed either** —
    everything below the cut comes back UNVERIFIED, which is the honest carrier
    of that negative result.

    Raises `FarFieldInputError` on any retrieval it cannot judge — empty hits,
    a top hit carrying no vector distance, or a non-finite distance. See that
    exception for why none of the three is a verdict.
    """
    if not retrieval.hits:
        raise FarFieldInputError(
            "empty retrieval: no hits to judge. This is NOT out-of-domain — the "
            "vector arm may simply not have run (see `arm` in retrieve())."
        )

    distance = retrieval.hits[0].vector_distance
    if distance is None:
        raise FarFieldInputError(
            "top hit carries no vector_distance, so there is no distance to "
            "compare against FAR_FIELD_ABSTAIN_DISTANCE. The lexical-only arm "
            "produces this, and it is a caller error, not a far-field query."
        )

    if not math.isfinite(distance):
        raise FarFieldInputError(
            f"top-1 vector_distance is {distance!r}, which no comparison against "
            "FAR_FIELD_ABSTAIN_DISTANCE can order. NaN in particular compares "
            "False against every bound, so a bare `>=` would silently answer."
        )

    verdict: FarFieldVerdict = (
        "ABSTAIN_OUT_OF_DOMAIN" if distance >= FAR_FIELD_ABSTAIN_DISTANCE else "ANSWER_UNVERIFIED"
    )
    return FarFieldDecision(verdict=verdict, top1_distance=distance)


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
    """Exact vector search. Embeds as `input_type="query"` — the corpus was
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
    rrf_k: int = RRF_K,
) -> dict[str, tuple[float, int | None, float | None, int | None, float | None]]:
    """Rank-based fusion. Returns chunk_id -> (rrf_score, lex_rank, lex_score,
    vec_rank, vec_distance). A chunk found by both arms merges into one entry
    carrying both ranks — dedupe is by chunk_id only, never by span overlap:
    the chunker's own 300-char overlap between adjacent chunks means a
    span-overlap rule would drop the neighbour chunk that q11-q13 in the
    golden set specifically need retrieved alongside it.

    `rrf_k` overrides the module default for this call only — a mechanism
    for the CLI's `--rrf-k` flag, not a re-tune of the shipped default.
    When one of the two hit lists is empty (the CLI's lexical-only/
    vector-only modes), the formula degenerates correctly on its own: with
    only one arm contributing, `1/(rrf_k+rank)` is monotonic in that arm's
    rank, so the fused order exactly reproduces that arm's own order.
    """
    fused: dict[str, tuple[float, int | None, float | None, int | None, float | None]] = {}

    for rank, lhit in enumerate(lexical_hits, start=1):
        score = 1.0 / (rrf_k + rank)
        fused[lhit.chunk_id] = (score, rank, lhit.score, None, None)

    for rank, vhit in enumerate(vector_hits, start=1):
        score = 1.0 / (rrf_k + rank)
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
    arm: Literal["hybrid", "lexical", "vector"] = "hybrid",
    rrf_k: int = RRF_K,
    reranker: Reranker = NOOP_RERANKER,
    rerank_n: int = RERANK_N,
) -> Retrieval:
    """Fused hybrid retrieval, then reranking. Both arms fetch `candidate_k` so
    RRF has depth to fuse; fusion is cut to `rerank_n`, reranked, then cut to
    `top_k`. `query_vector` passes through to `vector_search` — see its
    docstring.

    `arm` restricts which retriever(s) actually run: `"lexical"` skips
    `vector_search` entirely — no embedding call, genuinely $0 — and
    `"vector"` skips `lexical_search`. `"hybrid"` (default) runs both and
    fuses, unchanged from before this parameter existed. `rrf_k` passes
    through to `_rrf_fuse` — see its docstring.

    `reranker` defaults to `NOOP_RERANKER`, which is load-bearing rather than
    merely convenient: with it, this function's output is **byte-identical** to
    the pre-reranker implementation, which is what makes chunk 5's no-rerank
    arm a genuine baseline instead of a second code path. `_demo()` asserts
    that against a pinned digest whose lineage runs back to commit `5db0925`,
    before this parameter existed. `RerankError` is deliberately *not* caught here — see its
    docstring for why the policy belongs at each boundary instead.
    """
    lexical_hits = (
        await lexical_search(session, query, candidate_k, document_ids)
        if arm in ("hybrid", "lexical")
        else []
    )
    vector_hits = (
        await vector_search(
            vector_store, embedder, query, candidate_k, document_ids, query_vector=query_vector
        )
        if arm in ("hybrid", "vector")
        else []
    )
    fused = _rrf_fuse(lexical_hits, vector_hits, rrf_k=rrf_k)

    by_id: dict[str, _LexicalHit | _VectorHit] = {}
    for lhit in lexical_hits:
        by_id[lhit.chunk_id] = lhit
    for vhit in vector_hits:
        by_id.setdefault(vhit.chunk_id, vhit)

    # Cut to the rerank pool, not to `top_k` — the reranker has to see N
    # candidates to reorder among them. `max(..., top_k)` so a caller who asks
    # for more results than the pool can never get a silently short list.
    #
    # ponytail: with NoOpReranker this builds N objects and then discards all
    # but top_k. Deliberate — branching on "is there a reranker" would create
    # the second code path chunk 5 must not have. The waste is bounded (one
    # wider IN, a few dozen dataclasses) and invisible next to the vector query.
    pool_size = max(rerank_n, top_k)
    ranked_ids = sorted(fused, key=lambda cid: fused[cid][0], reverse=True)[:pool_size]
    if not ranked_ids:
        return Retrieval(hits=[])

    doc_ids = {by_id[cid].document_id for cid in ranked_ids}
    stmt = select(Document.id, Document.filename).where(Document.id.in_(doc_ids))
    rows = (await session.execute(stmt)).all()
    filenames = {row.id: row.filename for row in rows}

    candidates: list[RetrievedChunk] = []
    for cid in ranked_ids:
        matched_hit = by_id[cid]
        rrf_score, lex_rank, lex_score, vec_rank, vec_distance = fused[cid]
        chunk_text = matched_hit.text
        if chunk_text is None:
            # Vector arm returned no body for this chunk — shouldn't happen,
            # since the frozen corpus manifest carries the document text, but
            # fail loudly rather than emit a hit the Verifier can't score.
            raise RuntimeError(f"chunk {cid} has no text from either retrieval arm")
        candidates.append(
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

    reranked = await reranker.rerank(query, candidates, top_k)
    by_chunk = {c.chunk_id: c for c in candidates}
    hits: list[RetrievedChunk] = []
    for rank, cid in enumerate(reranked.order, start=1):
        candidate = by_chunk[cid]
        score = reranked.scores.get(cid)
        # No score means nothing reranked this hit, so both fields stay None and
        # the candidate object is passed through untouched — not even copied.
        # That is what makes the NoOp path byte-identical rather than merely
        # equivalent.
        hits.append(
            candidate if score is None else replace(candidate, rerank_score=score, rerank_rank=rank)
        )
    return Retrieval(hits=hits, rerank=reranked.timing)


# --- the chunk 5 guarantee ---------------------------------------------------
# The no-rerank benchmark arm is only a real baseline if it is the *same* code
# path as the reranked one. These pin that. The digest's lineage runs back to
# the pre-reranker implementation at 5db0925 — see the note above the constant
# for what changed when the vector backend was replaced, and what was measured
# before the value was re-pinned.

_PRE_RERANK_COMMIT = "5db0925"
_BASELINE_QIDS = ("a01", "a05", "a09", "a13", "a17", "a21", "a25", "u01")

# sha256 over every pre-rerank field of every hit, for the 8 questions above in
# both arms at top_k=40/candidate_k=30 — 560 hits.
#
# RE-PINNED 2026-08-31, when the vector arm moved from HNSW to exact brute-force
# search. The previous value, 3ffa60b3…89cfa3a, was captured 2026-08-21 against
# commit 5db0925 and cannot be regenerated any more: 5db0925's vector arm was
# Chroma/HNSW, which no longer exists in this tree.
#
# What the re-pin costs, measured before it was taken (chunk 7.1g §4a): across
# all 8 questions x 2 arms, exact and HNSW agreed on every id, every rank, every
# membership and every RRF score — `ANY id-order difference: False`. The only
# thing that moved was the *string* of vector_distance, on 218 of 560 hits, by
# one float32 ULP. `_quantize_distance` now cuts that field to 9dp so a
# last-bit representation difference cannot reach the hash; the value below is
# the post-quantization digest under exact search, confirmed identical across
# three separate OS processes.
#
# So this is still ground truth about behaviour this code did not choose — what
# it can no longer do is prove byte-identity with a commit whose backend is
# gone. If it fails, the wiring changed retrieval behaviour and chunk 5's
# no-rerank arm is no longer comparable to the chunk 3 baseline. It is still not
# a snapshot to re-bless on a whim: a failure is a finding, not a refresh.
_PRE_RERANK_DIGEST = "03bc2840d2affc458d3adffdfcc613c0839b6ab7d14b2207439d46d1456d5e1f"


def _quantize_distance(distance: float | None) -> str:
    """`vector_distance` at fixed 9dp, instead of `repr()`.

    `repr()` round-trips a float exactly, which is the right thing for every
    other field here and the wrong thing for this one: it makes the digest
    sensitive to the last bit of a float32 summation, so re-deriving the same
    distance by a different accumulation order changes the hash while changing
    no id, no rank, no membership and no RRF score. Under the HNSW arm 218 of
    560 hits carried a last-bit-different distance string across runs of the
    same code (chunk 7.1g §4a).

    9dp, not fewer: measured across the 36 golden questions' top-40 lists, the
    tightest gap between two adjacent distances is 3.58e-07 and the 1st
    percentile is 4.01e-05, so a 1e-09 grid cannot collide two genuinely
    different results.

    It does NOT absorb float32 accumulation noise, and is not meant to: that
    noise reaches ~6e-07 here, wider than 1e-09 and wider than the tightest
    adjacent gap. What makes this digest reproducible is that `ExactVectorStore`
    is bitwise deterministic (`test_vector_search_stability.py`); the
    quantization only removes sensitivity to representation below 1e-09.
    """
    return "None" if distance is None else f"{distance:.9f}"


def _serialize_pre_rerank_fields(hits: list[RetrievedChunk]) -> str:
    """Every field `RetrievedChunk` had before the reranker existed.

    `rerank_score`/`rerank_rank` are excluded on purpose — they did not exist
    at 5db0925, so including them could not match. That they stay None on this
    path is asserted separately, which together means: everything that existed
    is unchanged, and everything new is provably inert.
    """
    return "\n".join(
        "|".join(
            (
                h.chunk_id,
                h.document_id,
                h.document_name,
                repr(h.page),
                repr(h.char_start),
                repr(h.char_end),
                repr(len(h.text)),
                hashlib.sha256(h.text.encode()).hexdigest()[:16],
                repr(h.rrf_score),  # repr() round-trips a float exactly
                repr(h.lexical_rank),
                repr(h.lexical_score),
                repr(h.vector_rank),
                _quantize_distance(h.vector_distance),
            )
        )
        for h in hits
    )


async def _assert_noop_matches_pre_rerank_baseline(
    session: AsyncSession, vector_store: VectorStore
) -> None:
    from app.core.config import get_settings
    from app.services.embeddings import VoyageEmbedder
    from app.services.evaluation import GOLDEN_SET_PATH, load_golden_set, load_query_vectors

    settings = get_settings()
    embedder = VoyageEmbedder(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.voyage_model,
        dimensions=settings.voyage_dimensions,
    )
    by_id = {e.id: e for e in load_golden_set(GOLDEN_SET_PATH)}
    # allow_network defaults to False: a cache miss raises rather than spends.
    vectors = await load_query_vectors(
        {qid: by_id[qid].question for qid in _BASELINE_QIDS}, embedder
    )

    blocks: list[str] = []
    for qid in _BASELINE_QIDS:
        for arm in ("vector", "hybrid"):
            result = await retrieve(
                by_id[qid].question,
                session,
                vector_store,
                embedder,
                top_k=40,
                candidate_k=30,
                query_vector=vectors[qid],
                arm=arm,
                reranker=NOOP_RERANKER,
            )
            assert all(h.rerank_score is None and h.rerank_rank is None for h in result.hits), (
                f"{qid}/{arm}: NoOp must not populate the rerank fields"
            )
            assert result.rerank is not None and result.rerank.infer_ms == 0.0
            blocks.append(
                f"### {qid} {arm} n={len(result.hits)}\n{_serialize_pre_rerank_fields(result.hits)}"
            )

    digest = hashlib.sha256("\n".join(blocks).encode()).hexdigest()
    assert digest == _PRE_RERANK_DIGEST, (
        f"NoOp retrieval diverged from the pinned pre-rerank baseline "
        f"(lineage: commit {_PRE_RERANK_COMMIT}).\n"
        f"  expected {_PRE_RERANK_DIGEST}\n  actual   {digest}\n"
        "The wiring changed retrieval behaviour. Chunk 5's no-rerank arm is no "
        "longer a baseline until this is explained."
    )


def _demo() -> None:
    import asyncio

    from app.core.config import get_settings
    from app.db.session import create_engine, create_session_factory
    from app.services.embeddings import FakeEmbedder
    from app.services.vector_store import ExactVectorStore

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
        # a fake query vector still exercises the search path and the join/provenance
        # code, it just won't rank meaningfully; that's proven separately by evaluation.py
        # against the cached real query vectors.
        vector_store = ExactVectorStore()

        async with session_factory() as session:
            result = await retrieve(
                "What does the glossary say about Cross-Encoder rerankers?",
                session,
                vector_store,
                embedder,
                top_k=5,
                candidate_k=20,
            )
            hits = result.hits
            assert hits, "expected at least one hit against the real corpus"
            assert len(hits) == 5, f"top_k must still cut to 5, got {len(hits)}"
            for h in hits:
                assert h.document_id and h.page > 0
                assert h.char_end > h.char_start
                assert h.lexical_rank is not None or h.vector_rank is not None
                assert h.rerank_score is None and h.rerank_rank is None

            # Offset round-trip: text must be an exact slice of the stored chunk row.
            row = (
                await session.execute(
                    text("SELECT text FROM chunks WHERE id = :id"), {"id": hits[0].chunk_id}
                )
            ).scalar_one()
            assert row == hits[0].text

            await _assert_noop_matches_pre_rerank_baseline(session, vector_store)

        vector_store.close()
        await engine.dispose()

    asyncio.run(run_live())
    print(
        "ok: RRF math, dedupe-by-id, determinism, a live hybrid retrieve(), and the NoOp "
        f"path matching the pinned pre-rerank baseline (lineage: {_PRE_RERANK_COMMIT}) "
        f"across {len(_BASELINE_QIDS)} questions x 2 arms"
    )


if __name__ == "__main__":
    _demo()
