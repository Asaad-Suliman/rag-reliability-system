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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.embeddings import Embedder
from app.services.retrieval import (
    DEFAULT_CANDIDATE_K,
    RRF_K,
    lexical_search,
    retrieve,
    vector_search,
)
from app.services.vector_store import VectorStore

GOLDEN_SET_PATH = Path("tests/fixtures/golden_set.json")
QUERY_EMBEDDINGS_CACHE_PATH = Path("tests/fixtures/query_embeddings.json")
RECALL_KS = (5, 10)


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
    each have two gold chunks and must be scored as such. MRR uses the first
    gold chunk to appear in the ranking, 0.0 if none does.
    """
    recall_at = {
        k: len(set(ranked_chunk_ids[:k]) & gold_chunk_ids) / len(gold_chunk_ids) for k in RECALL_KS
    }
    first_hit_rank = next(
        (i + 1 for i, cid in enumerate(ranked_chunk_ids) if cid in gold_chunk_ids), None
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


async def evaluate(
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    golden_set_path: Path = GOLDEN_SET_PATH,
    cache_path: Path = QUERY_EMBEDDINGS_CACHE_PATH,
    candidate_k: int = DEFAULT_CANDIDATE_K,
    rrf_k: int = RRF_K,
    allow_network: bool = False,
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
    golden_set: dict[str, Any] = json.loads(golden_set_path.read_text())
    questions: list[dict[str, Any]] = golden_set["questions"]
    answerable = [q for q in questions if q["type"] != "unanswerable"]
    unanswerable = [q for q in questions if q["type"] == "unanswerable"]

    questions_by_id = {q["id"]: q["question"] for q in questions}
    vectors = await load_query_vectors(
        questions_by_id, embedder, cache_path, allow_network=allow_network
    )

    lexical_scores: list[tuple[dict[int, float], float]] = []
    vector_scores: list[tuple[dict[int, float], float]] = []
    hybrid_scores: list[tuple[dict[int, float], float]] = []

    for q in answerable:
        query = q["question"]
        gold = {cid for ev in q["evidence"] for cid in ev["chunk_ids"]}
        query_vector = vectors[q["id"]]

        lexical_hits = await lexical_search(session, query, candidate_k, None)
        vector_hits = await vector_search(
            vector_store, embedder, query, candidate_k, None, query_vector=query_vector
        )
        hybrid_hits = await retrieve(
            query,
            session,
            vector_store,
            embedder,
            top_k=candidate_k,
            candidate_k=candidate_k,
            query_vector=query_vector,
            rrf_k=rrf_k,
        )

        lexical_scores.append(_rank_metrics([h.chunk_id for h in lexical_hits], gold))
        vector_scores.append(_rank_metrics([h.chunk_id for h in vector_hits], gold))
        hybrid_scores.append(_rank_metrics([h.chunk_id for h in hybrid_hits], gold))

    abstention_scores: dict[str, float] = {}
    for q in unanswerable:
        query = q["question"]
        hits = await retrieve(
            query,
            session,
            vector_store,
            embedder,
            top_k=1,
            candidate_k=candidate_k,
            query_vector=vectors[q["id"]],
            rrf_k=rrf_k,
        )
        abstention_scores[q["id"]] = hits[0].rrf_score if hits else 0.0

    return EvaluationReport(
        lexical=_aggregate(lexical_scores),
        vector=_aggregate(vector_scores),
        hybrid=_aggregate(hybrid_scores),
        abstention_scores=abstention_scores,
        rrf_k=rrf_k,
        candidate_k=candidate_k,
    )


def format_report(report: EvaluationReport) -> str:
    lines = [
        f"rrf_k={report.rrf_k} candidate_k={report.candidate_k}",
        f"{'arm':<10} {'n':>3}  " + "  ".join(f"recall@{k:<3}" for k in RECALL_KS) + "     mrr",
    ]
    arms = (("lexical", report.lexical), ("vector", report.vector), ("hybrid", report.hybrid))
    for name, metrics in arms:
        recalls = "  ".join(f"{metrics.recall_at[k]:>9.3f}" for k in RECALL_KS)
        lines.append(f"{name:<10} {metrics.n_questions:>3}  {recalls}  {metrics.mrr:>6.3f}")
    lines.append("")
    lines.append("abstention (unanswerable questions, top-1 hybrid rrf_score, lower = better):")
    for qid, score in report.abstention_scores.items():
        lines.append(f"  {qid}: {score:.5f}")
    return "\n".join(lines)


def _demo() -> None:
    """Offline: exercises the recall/MRR math and the cache round-trip against
    a FakeEmbedder and a scratch cache file — no real corpus, no network, no
    dependence on the live Postgres/Chroma state.
    """
    import asyncio
    import tempfile

    # --- recall/MRR math on hand-built rankings ---
    gold = {"chk_a", "chk_b"}
    recall_at, mrr = _rank_metrics(["chk_x", "chk_a", "chk_y", "chk_b", "chk_z"], gold)
    assert recall_at[5] == 1.0, recall_at  # both gold chunks present in top 5
    assert recall_at == {5: 1.0, 10: 1.0}
    assert mrr == 1 / 2, mrr  # first gold chunk (chk_a) at rank 2

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
    print("ok: recall@k/MRR math, cache-miss refusal, and cache round-trip verified offline")


if __name__ == "__main__":
    _demo()
