"""Golden-set evaluation: recall@k and MRR for lexical-only, vector-only, and
hybrid retrieval — Step 02 DoD #5 ("hybrid measurably beats vector-only").

Query vectors are embedded exactly once per (question, model, dimensions) and
cached to `tests/fixtures/query_embeddings.json`. Every subsequent run —
including k-tuning and this module's own `_demo()` — reads the cache and makes
zero network calls. See `evaluate()`'s docstring for the one-time cost.
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
    questions: list[str],
    embedder: Embedder,
    cache_path: Path = QUERY_EMBEDDINGS_CACHE_PATH,
) -> dict[str, list[float]]:
    """One vector per question, keyed by content+model+dimensions so a model
    change can't silently serve a stale vector. Only the questions missing
    from the cache are ever embedded — a fresh cache costs one network call
    for the whole golden set, every rerun after that costs zero.
    """
    cache: dict[str, list[float]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    keys = {q: _cache_key(q, embedder.model, embedder.dimensions) for q in questions}
    missing = [q for q in questions if keys[q] not in cache]
    if missing:
        vectors = await embedder.embed(missing, input_type="query")
        for q, vector in zip(missing, vectors, strict=True):
            cache[keys[q]] = vector
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))

    return {q: cache[keys[q]] for q in questions}


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


async def evaluate(
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    golden_set_path: Path = GOLDEN_SET_PATH,
    cache_path: Path = QUERY_EMBEDDINGS_CACHE_PATH,
    candidate_k: int = DEFAULT_CANDIDATE_K,
) -> EvaluationReport:
    """Run all three retrieval arms over the golden set.

    Cost (measured, one-time, only when the cache is cold): 13 questions,
    1,337 chars, ~330 tokens (char/4 estimate) in **one** Voyage request —
    well under both the 3 RPM and 10K TPM caps on the throttled account, so no
    inter-request spacing is needed. At $0.02/1M tokens that's ~$0.0000066,
    and inside the 200M-token free tier regardless. Every subsequent call to
    this function reads the cache and makes zero network calls.
    """
    golden_set: dict[str, Any] = json.loads(golden_set_path.read_text())
    questions: list[dict[str, Any]] = golden_set["questions"]
    answerable = [q for q in questions if q["type"] != "unanswerable"]
    unanswerable = [q for q in questions if q["type"] == "unanswerable"]

    vectors = await load_query_vectors([q["question"] for q in questions], embedder, cache_path)

    lexical_scores: list[tuple[dict[int, float], float]] = []
    vector_scores: list[tuple[dict[int, float], float]] = []
    hybrid_scores: list[tuple[dict[int, float], float]] = []

    for q in answerable:
        query = q["question"]
        gold = {cid for ev in q["evidence"] for cid in ev["chunk_ids"]}
        query_vector = vectors[query]

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
            query_vector=vectors[query],
        )
        abstention_scores[q["id"]] = hits[0].rrf_score if hits else 0.0

    return EvaluationReport(
        lexical=_aggregate(lexical_scores),
        vector=_aggregate(vector_scores),
        hybrid=_aggregate(hybrid_scores),
        abstention_scores=abstention_scores,
    )


def format_report(report: EvaluationReport) -> str:
    lines = [
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
            questions = ["what is RAG?", "what is a reranker?"]

            vectors1 = await load_query_vectors(questions, fake, cache_path)
            assert cache_path.exists()
            written = json.loads(cache_path.read_text())
            assert len(written) == 2

            # Second call must not add new entries or change existing ones —
            # this is the "reruns are free" guarantee the eval harness relies on.
            vectors2 = await load_query_vectors(questions, fake, cache_path)
            assert vectors1 == vectors2
            assert json.loads(cache_path.read_text()) == written

            # A new question extends the cache without disturbing the old entries.
            vectors3 = await load_query_vectors([*questions, "new question"], fake, cache_path)
            assert vectors3["what is RAG?"] == vectors1["what is RAG?"]
            assert len(json.loads(cache_path.read_text())) == 3

    asyncio.run(run_cache_check())
    print("ok: recall@k/MRR math and query-vector cache round-trip verified offline")


if __name__ == "__main__":
    _demo()
