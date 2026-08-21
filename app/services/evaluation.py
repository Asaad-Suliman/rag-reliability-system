"""Golden-set evaluation: recall@k and MRR for lexical-only, vector-only, and
hybrid retrieval — Step 02 DoD #5 ("hybrid measurably beats vector-only").

Query vectors are embedded once per (question, model, dimensions) and cached
to `tests/fixtures/query_embeddings.json`. Network access is opt-in: a cache
miss raises unless the caller passes `allow_network=True` (see
`load_query_vectors`), so a plain `evaluate()` call can never surprise-spend.
Every run against a warm cache — including k-tuning and this module's own
`_demo()` — makes zero network calls. See `evaluate()`'s docstring for the
one-time cost.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.embeddings import Embedder
from app.services.reranking import NOOP_RERANKER, RERANK_N, Reranker
from app.services.retrieval import (
    DEFAULT_CANDIDATE_K,
    RRF_K,
    retrieve,
)
from app.services.vector_store import VectorStore

GOLDEN_SET_PATH = Path("tests/fixtures/golden_set_v3.json")
QUERY_EMBEDDINGS_CACHE_PATH = Path("tests/fixtures/query_embeddings.json")
RECALL_KS = (5, 10, 30)

# MRR is cut at a FIXED depth, independent of the caller's `top_k`.
#
# Before chunk 4 it was unbounded, which meant it silently equalled whatever
# `top_k` the caller passed. That is not a comparable metric: raising the rerank
# fan-out from 30 to 40 moved hybrid's MRR from 0.4504 to 0.4513 purely because
# a13's gold chunk sits at rank 35 and became visible — retrieval order was
# byte-identical. Chunk 5 compares four arms; if any two ran at different
# `top_k`, their MRRs would differ for that reason alone and nothing in the
# report would say so.
#
# 10 rather than 30 or 40: MRR weights rank 1 at 1.0 and rank 30 at 0.033, so
# depth past ~10 contributes noise, not signal — and precision-at-1 is exactly
# what the reranker exists to fix. The conventional reporting depth too.
#
# Changing this re-baselines the metric. It is a definition change, not drift:
# the chunk 3 figures (vector 0.707, hybrid 0.450) were computed unbounded at
# depth 30 and do not reproduce at 10 by construction.
MRR_DEPTH = 10


class GoldenSetError(RuntimeError):
    """A golden set could not be resolved against the live corpus.

    Always raised, never swallowed: a gold set that silently resolves to the
    wrong chunks produces plausible metrics that are quietly meaningless, which
    is strictly worse than a run that stops.
    """


@dataclass(frozen=True)
class GoldenEntry:
    """One golden-set entry, normalised across schema versions.

    `spans` are (char_start, char_end) pairs into the document's canonical text.
    Gold chunk ids are *not* stored here and never read from the fixture — they
    are resolved from these spans at evaluation time (see
    `resolve_gold_chunk_ids`). v2 recorded `chunk_ids` directly, which are
    `chk_` ULIDs minted fresh on every ingest, so a single reindex silently
    zeroed the gold set while the metrics kept reporting numbers.
    """

    id: str
    question: str
    answerable: bool
    document_id: str
    spans: tuple[tuple[int, int], ...]


def load_golden_set(path: Path) -> list[GoldenEntry]:
    """Read v2 or v3 into one shape. v2 is still loadable so the v2 -> v3 delta
    can be measured on identical scoring code — otherwise a change in the
    numbers could be the benchmark or the harness, and we could not tell which.
    """
    raw: dict[str, Any] = json.loads(path.read_text())
    version = raw.get("version")
    spans: tuple[tuple[int, int], ...]

    if version == 3:
        entries = []
        for e in raw["entries"]:
            spans = (
                ((e["char_start"], e["char_end"]),)
                if e["answerable"]
                else ((e["near_miss_to"]["char_start"], e["near_miss_to"]["char_end"]),)
            )
            entries.append(
                GoldenEntry(e["id"], e["question"], e["answerable"], e["document_id"], spans)
            )
        return entries

    if version == 2:
        doc = raw["corpus"]["document_id"]
        entries = []
        for q in raw["questions"]:
            answerable = q["type"] != "unanswerable"
            spans = tuple((ev["char_start"], ev["char_end"]) for ev in q.get("evidence", []))
            entries.append(GoldenEntry(q["id"], q["question"], answerable, doc, spans))
        return entries

    raise GoldenSetError(f"{path}: unsupported golden set version {version!r}")


async def resolve_gold_chunk_ids(
    session: AsyncSession, entries: list[GoldenEntry]
) -> dict[str, set[str]]:
    """Map each entry id to the chunk ids its spans fall inside.

    Exact rather than approximate: `chunking.py` guarantees
    `canonical_text[c.char_start:c.char_end] == c.text`, so a span contained in
    a chunk is genuinely that chunk's text. Chunks do not tile the canonical
    text (246 of 259 consecutive pairs have gaps), so a span that crosses a
    boundary has no owner — that raises rather than resolving to nothing.
    """
    by_doc: dict[str, list[Any]] = {}
    for doc in {e.document_id for e in entries}:
        by_doc[doc] = list(
            (
                await session.execute(
                    text(
                        "SELECT id, char_start, char_end FROM chunks "
                        "WHERE document_id = :d ORDER BY char_start"
                    ),
                    {"d": doc},
                )
            ).all()
        )
        if not by_doc[doc]:
            raise GoldenSetError(f"no chunks in the live corpus for document {doc!r}")

    resolved: dict[str, set[str]] = {}
    for entry in entries:
        ids: set[str] = set()
        for start, end in entry.spans:
            owners = [
                c for c in by_doc[entry.document_id] if c.char_start <= start and c.char_end >= end
            ]
            if not owners:
                raise GoldenSetError(
                    f"{entry.id}: span [{start},{end}) is not contained in any chunk of "
                    f"{entry.document_id} — it crosses a chunk boundary or falls outside the corpus"
                )
            ids.add(owners[0].id)
        if entry.answerable and not ids:
            raise GoldenSetError(f"{entry.id}: answerable entry resolved to no gold chunks")
        resolved[entry.id] = ids
    return resolved


@dataclass(frozen=True)
class ArmMetrics:
    recall_at: dict[int, float]
    mrr: float
    n_questions: int


def _cache_key(question: str, model: str, dimensions: int) -> str:
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()
    return f"{digest}:{model}:{dimensions}"


async def load_query_vectors(
    questions: dict[str, str],
    embedder: Embedder,
    cache_path: Path = QUERY_EMBEDDINGS_CACHE_PATH,
    allow_network: bool = False,
) -> dict[str, list[float]]:
    """One vector per question, keyed by the same id `questions` uses (e.g.
    the golden set's `q01`). Cache entries are keyed by content+model+
    dimensions so a model change can't silently serve a stale vector.

    `allow_network` gates the only place this module can spend money.
    Defaults to False: a cache miss raises rather than silently calling the
    embedding API. Pass `allow_network=True` with a real Embedder to embed
    and cache the missing questions — after that, every run is a cache hit
    and this flag has no effect.
    """
    cache: dict[str, list[float]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    keys = {
        qid: _cache_key(text, embedder.model, embedder.dimensions)
        for qid, text in questions.items()
    }
    missing = [qid for qid in questions if keys[qid] not in cache]

    if missing and not allow_network:
        raise RuntimeError(
            f"query-embedding cache miss for {len(missing)} question(s) not covered by "
            f"{cache_path}: {', '.join(sorted(missing))}. Refusing to call the embedding "
            "API by default. Pass allow_network=True (with a real Embedder) to embed and "
            "cache them — this is the only place in this module that can spend money."
        )

    if missing:
        texts = [questions[qid] for qid in missing]
        vectors = await embedder.embed(texts, input_type="query")
        for qid, vector in zip(missing, vectors, strict=True):
            cache[keys[qid]] = vector
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))

    return {qid: cache[keys[qid]] for qid in questions}


def _rank_metrics(
    ranked_chunk_ids: list[str], gold_chunk_ids: set[str]
) -> tuple[dict[int, float], float]:
    """True recall (fraction of gold chunks retrieved), not hit-rate — q11-q13
    each have two gold chunks and must be scored as such.

    MRR uses the first gold chunk inside `MRR_DEPTH`, 0.0 if none appears there.
    Cutting at a fixed depth is the whole point: see `MRR_DEPTH`.
    """
    recall_at = {
        k: len(set(ranked_chunk_ids[:k]) & gold_chunk_ids) / len(gold_chunk_ids) for k in RECALL_KS
    }
    first_hit_rank = next(
        (i + 1 for i, cid in enumerate(ranked_chunk_ids[:MRR_DEPTH]) if cid in gold_chunk_ids),
        None,
    )
    mrr = 1.0 / first_hit_rank if first_hit_rank else 0.0
    return recall_at, mrr


def _aggregate(per_question: list[tuple[dict[int, float], float]]) -> ArmMetrics:
    n = len(per_question)
    recall_at = {k: sum(r[k] for r, _ in per_question) / n for k in RECALL_KS}
    mrr = sum(m for _, m in per_question) / n
    return ArmMetrics(recall_at=recall_at, mrr=mrr, n_questions=n)


@dataclass(frozen=True)
class EvaluationReport:
    lexical: ArmMetrics
    vector: ArmMetrics
    hybrid: ArmMetrics
    abstention_scores: dict[str, float]  # unanswerable question id -> top-1 rrf_score
    rrf_k: int
    candidate_k: int
    golden_set_path: str
    golden_set_version: int
    # Carried explicitly so a report can never be read as covering more
    # questions than it scored. recall/MRR are answerable-only by construction.
    n_answerable: int
    n_unanswerable: int
    # Which reranker produced this run, and how deep MRR was cut. Every run
    # today is NoOpReranker, so both are currently constant — they exist for
    # chunk 5, which runs four arms. A benchmark row that does not name its
    # reranker cannot be told apart from one that does, and the whole point of
    # chunk 5 is comparing reranked against not.
    reranker: str
    mrr_depth: int
    rerank_n: int


async def evaluate(
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    golden_set_path: Path = GOLDEN_SET_PATH,
    cache_path: Path = QUERY_EMBEDDINGS_CACHE_PATH,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    rrf_k: int = RRF_K,
    allow_network: bool = False,
    reranker: Reranker = NOOP_RERANKER,
    rerank_n: int = RERANK_N,
) -> EvaluationReport:
    """Run all three retrieval arms over the golden set.

    Cost (measured, one-time, only when the cache is cold and `allow_network`
    is passed): 13 questions, 1,337 chars, ~330 tokens (char/4 estimate) in
    **one** Voyage request — well under both the 3 RPM and 10K TPM caps on
    the throttled account, so no inter-request spacing is needed. At
    $0.02/1M tokens that's ~$0.0000066, and inside the 200M-token free tier
    regardless. Every subsequent call to this function reads the cache and
    makes zero network calls — see `load_query_vectors`'s docstring for the
    `allow_network` gate.
    """
    version = int(json.loads(golden_set_path.read_text()).get("version", 0))
    entries = load_golden_set(golden_set_path)
    # The split is read off the schema, never inferred from a type string:
    # unanswerable entries must never enter recall or MRR.
    answerable = [e for e in entries if e.answerable]
    unanswerable = [e for e in entries if not e.answerable]
    gold_by_id = await resolve_gold_chunk_ids(session, entries)

    questions_by_id = {e.id: e.question for e in entries}
    vectors = await load_query_vectors(
        questions_by_id, embedder, cache_path, allow_network=allow_network
    )

    # Every arm goes through retrieve(), including lexical and vector.
    #
    # They used to call lexical_search()/vector_search() directly, which was fine
    # while retrieval was the only stage — but the reranker lives in retrieve(),
    # so the direct calls silently skipped it and `--reranker local` reranked the
    # hybrid arm alone. That made chunk 5's "vector + rerank" arm unproducible,
    # and worse, would have compared a reranked hybrid against an un-reranked
    # vector-only and read the difference as a fusion result.
    #
    # Safe for the no-rerank baseline: _rrf_fuse() degenerates to the single
    # arm's own order when the other hit list is empty (see its docstring), so
    # with NoOpReranker every recorded figure is unchanged — asserted by the
    # regression gate, not assumed.
    async def arm_hits(
        query: str,
        query_vector: list[float],
        arm: Literal["hybrid", "lexical", "vector"],
    ) -> list[str]:
        result = await retrieve(
            query,
            session,
            vector_store,
            embedder,
            top_k=rerank_n,
            candidate_k=candidate_k,
            query_vector=query_vector,
            arm=arm,
            rrf_k=rrf_k,
            reranker=reranker,
            rerank_n=rerank_n,
        )
        return [h.chunk_id for h in result.hits]

    lexical_scores: list[tuple[dict[int, float], float]] = []
    vector_scores: list[tuple[dict[int, float], float]] = []
    hybrid_scores: list[tuple[dict[int, float], float]] = []

    for q in answerable:
        query = q.question
        gold = gold_by_id[q.id]
        query_vector = vectors[q.id]

        lexical_scores.append(_rank_metrics(await arm_hits(query, query_vector, "lexical"), gold))
        vector_scores.append(_rank_metrics(await arm_hits(query, query_vector, "vector"), gold))
        hybrid_scores.append(_rank_metrics(await arm_hits(query, query_vector, "hybrid"), gold))

    abstention_scores: dict[str, float] = {}
    for q in unanswerable:
        abstention = await retrieve(
            q.question,
            session,
            vector_store,
            embedder,
            top_k=1,
            candidate_k=candidate_k,
            query_vector=vectors[q.id],
            rrf_k=rrf_k,
            reranker=reranker,
            rerank_n=rerank_n,
        )
        # Still rrf_score, still top-1, so this column stays comparable to
        # chunk 3's. DECISIONS.md (2026-08-19) already records that rrf_score
        # cannot signal abstention at all; chunk 5 replaces this with the
        # reranker's score, and that is a chunk 5 decision, not a wiring one.
        hits = abstention.hits
        abstention_scores[q.id] = hits[0].rrf_score if hits else 0.0

    return EvaluationReport(
        lexical=_aggregate(lexical_scores),
        vector=_aggregate(vector_scores),
        hybrid=_aggregate(hybrid_scores),
        abstention_scores=abstention_scores,
        rrf_k=rrf_k,
        candidate_k=candidate_k,
        golden_set_path=str(golden_set_path),
        golden_set_version=version,
        n_answerable=len(answerable),
        n_unanswerable=len(unanswerable),
        reranker=type(reranker).__name__,
        mrr_depth=MRR_DEPTH,
        rerank_n=rerank_n,
    )


def format_report(report: EvaluationReport) -> str:
    lines = [
        f"golden set: {report.golden_set_path} (v{report.golden_set_version}) — "
        f"{report.n_answerable} answerable, {report.n_unanswerable} unanswerable",
        f"rrf_k={report.rrf_k} candidate_k={report.candidate_k} "
        f"rerank_n={report.rerank_n} reranker={report.reranker}",
        "",
        f"recall@k and MRR@{report.mrr_depth} over the {report.n_answerable} "
        "ANSWERABLE entries only:",
        f"{'arm':<10} {'n':>3}  "
        + "  ".join(f"recall@{k:<3}" for k in RECALL_KS)
        + f"  mrr@{report.mrr_depth}",
    ]
    arms = (("lexical", report.lexical), ("vector", report.vector), ("hybrid", report.hybrid))
    for name, metrics in arms:
        recalls = "  ".join(f"{metrics.recall_at[k]:>9.3f}" for k in RECALL_KS)
        lines.append(f"{name:<10} {metrics.n_questions:>3}  {recalls}  {metrics.mrr:>6.3f}")
    lines.append("")
    lines.append(
        f"abstention over the {report.n_unanswerable} UNANSWERABLE entries "
        "(top-1 hybrid rrf_score, lower = better) — scored separately, never folded "
        "into recall or MRR:"
    )
    for qid, score in report.abstention_scores.items():
        lines.append(f"  {qid}: {score:.5f}")
    return "\n".join(lines)


def _demo() -> None:
    """Two parts.

    Offline: recall/MRR math and the cache round-trip against a FakeEmbedder and
    a scratch cache file — no corpus, no network.

    Live (Postgres only, no Chroma, no embedding call, $0): gold-chunk
    resolution. The decisive check is that resolving v2's spans reproduces the
    `chunk_ids` v2 recorded independently at authoring time — ground truth this
    code did not produce. Then five deliberately broken spans confirm the
    resolver raises rather than quietly resolving to nothing, because a scoring
    path that silently reads wrong is worse than one that crashes.
    """
    import asyncio
    import tempfile

    # --- recall/MRR math on hand-built rankings ---
    gold = {"chk_a", "chk_b"}
    recall_at, mrr = _rank_metrics(["chk_x", "chk_a", "chk_y", "chk_b", "chk_z"], gold)
    assert recall_at[5] == 1.0, recall_at  # both gold chunks present in top 5
    # Keyed off RECALL_KS, not a literal dict: recall@30 was added in chunk 4 as
    # the rerank ceiling, and a hardcoded dict breaks on the next depth added
    # rather than adapting to it.
    assert recall_at == dict.fromkeys(RECALL_KS, 1.0), recall_at
    assert mrr == 1 / 2, mrr  # first gold chunk (chk_a) at rank 2

    # --- MRR is cut at MRR_DEPTH, recall is not ---
    # A gold chunk just inside the cut counts; one just outside scores 0.0 for
    # MRR while still counting toward recall@30. This is the property that makes
    # MRR comparable across arms with different top_k — without it, deepening
    # the list silently raises MRR (chunk 4: hybrid 0.4504 -> 0.4513 from a13 at
    # rank 35 alone, with retrieval order byte-identical).
    filler = [f"chk_f{i}" for i in range(40)]
    inside = filler[: MRR_DEPTH - 1] + ["chk_a"] + filler[MRR_DEPTH - 1 :]
    outside = filler[:MRR_DEPTH] + ["chk_a"] + filler[MRR_DEPTH:]
    r_in, mrr_in = _rank_metrics(inside, gold)
    r_out, mrr_out = _rank_metrics(outside, gold)
    assert mrr_in == 1 / MRR_DEPTH, mrr_in  # gold at exactly MRR_DEPTH still counts
    assert mrr_out == 0.0, mrr_out  # one rank past the cut is invisible to MRR
    assert r_in[30] == r_out[30] == 0.5, (r_in, r_out)  # recall sees it either way

    recall_at2, mrr2 = _rank_metrics(["chk_x", "chk_y"], gold)
    assert recall_at2[5] == 0.0 and mrr2 == 0.0, (recall_at2, mrr2)  # neither gold chunk retrieved

    aggregated = _aggregate([(recall_at, mrr), (recall_at2, mrr2)])
    assert aggregated.recall_at[5] == 0.5, aggregated  # (1.0 + 0.0) / 2
    assert aggregated.mrr == 0.25, aggregated  # (0.5 + 0.0) / 2

    async def run_cache_check() -> None:
        from app.services.embeddings import FakeEmbedder

        fake = FakeEmbedder(dimensions=8)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            questions = {"q_rag": "what is RAG?", "q_rerank": "what is a reranker?"}

            # A miss without allow_network must refuse and name the missing
            # ids — never silently call the embedder, even a free one.
            try:
                await load_query_vectors(questions, fake, cache_path)
            except RuntimeError as exc:
                assert "q_rag" in str(exc) and "q_rerank" in str(exc), exc
            else:
                raise AssertionError("expected a cache-miss error without allow_network=True")
            assert not cache_path.exists(), "a refused miss must not touch the cache file"

            vectors1 = await load_query_vectors(questions, fake, cache_path, allow_network=True)
            assert cache_path.exists()
            written = json.loads(cache_path.read_text())
            assert len(written) == 2

            # Full cache hit: allow_network's default (False) must succeed and
            # must not add new entries or change existing ones — this is the
            # "reruns are free" guarantee the eval harness relies on.
            vectors2 = await load_query_vectors(questions, fake, cache_path)
            assert vectors1 == vectors2
            assert json.loads(cache_path.read_text()) == written

            # A new question is a genuine miss again -- needs allow_network=True
            # -- but extends the cache without disturbing the old entries.
            extended = {**questions, "q_new": "new question"}
            vectors3 = await load_query_vectors(extended, fake, cache_path, allow_network=True)
            assert vectors3["q_rag"] == vectors1["q_rag"]
            assert len(json.loads(cache_path.read_text())) == 3

    asyncio.run(run_cache_check())

    async def run_resolver_check() -> None:
        from app.core.config import get_settings
        from app.db.session import create_engine, create_session_factory

        engine = create_engine(get_settings())
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                # --- v2 cross-check: resolution must reproduce recorded ids ---
                v2_path = Path("tests/fixtures/golden_set.json")
                v2_raw = json.loads(v2_path.read_text())
                recorded = {
                    q["id"]: {cid for ev in q["evidence"] for cid in ev["chunk_ids"]}
                    for q in v2_raw["questions"]
                }
                v2 = load_golden_set(v2_path)
                resolved = await resolve_gold_chunk_ids(session, v2)
                for entry in v2:
                    assert resolved[entry.id] == recorded[entry.id], (
                        f"{entry.id}: resolved {resolved[entry.id]} != recorded "
                        f"{recorded[entry.id]}"
                    )
                multi = [e for e in v2 if len(e.spans) > 1]
                assert multi, "expected v2 to contain multi-span questions (q11-q13)"
                assert all(len(resolved[e.id]) == 2 for e in multi), (
                    "multi-span must map to 2 chunks"
                )

                # --- v3 resolves end to end, and the split is structural ---
                v3 = load_golden_set(GOLDEN_SET_PATH)
                assert sum(e.answerable for e in v3) == 30, "expected 30 answerable"
                assert sum(not e.answerable for e in v3) == 6, "expected 6 near-miss"
                r3 = await resolve_gold_chunk_ids(session, v3)
                assert all(r3[e.id] for e in v3 if e.answerable), "every answerable must resolve"

                # --- negative controls: each must raise, none may pass ---
                good = next(e for e in v3 if e.answerable)
                doc = good.document_id
                broken = [
                    ("crosses a chunk gap", GoldenEntry("bad1", "q", True, doc, ((0, 271256),))),
                    (
                        "past end of corpus",
                        GoldenEntry("bad2", "q", True, doc, ((999000, 999100),)),
                    ),
                    ("answerable, no spans", GoldenEntry("bad3", "q", True, doc, ())),
                    ("unknown document", GoldenEntry("bad4", "q", True, "doc_NOPE", ((0, 10),))),
                    (
                        "off-by-one past chunk end",
                        GoldenEntry("bad5", "q", True, doc, ((good.spans[0][0], 271257),)),
                    ),
                ]
                for label, entry in broken:
                    try:
                        await resolve_gold_chunk_ids(session, [entry])
                    except GoldenSetError:
                        pass
                    else:
                        raise AssertionError(f"resolver accepted a bad span: {label}")
        finally:
            await engine.dispose()

    asyncio.run(run_resolver_check())
    print(
        "ok: recall@k/MRR math and cache round-trip offline; gold resolution reproduces "
        "all 13 of v2's recorded chunk_id sets, v3 resolves 30+6, and 5 broken spans all raise"
    )


if __name__ == "__main__":
    _demo()
