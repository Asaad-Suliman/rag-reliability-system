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
from collections.abc import Callable
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
    Retrieval,
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

# How many hits the abstention decision actually consumes. Named rather than
# written as a literal in the retrieve() call below, because the *same* number
# defines what "the near_miss_to span was retrieved" means: the check has to be
# evaluated against the hits the decision saw, never against recall@10 or the
# rerank_n pool it never reads. One constant, so the two cannot drift apart.
ABSTENTION_TOP_K = 1

# The three outcomes a near-miss item can take.
#
# UNSCORED is the whole point — DECISIONS.md 2026-08-20 (golden set v3, Note 2)
# and the 2026-08-21 chunk 5 gate: a near-miss only tests abstention if the
# retriever actually surfaced the near_miss_to span. If it did not, the system
# abstained because retrieval found nothing, and counting that as a correct
# abstention is a retrieval failure scored as a pass — it inflates the rate and
# contaminates the Guardrail threshold fitted from it downstream.
ABSTAINED = "ABSTAINED"  # span retrieved, system abstained — correct
ANSWERED = "ANSWERED"  # span retrieved, system answered — confabulation, failure
UNSCORED = "UNSCORED"  # span not retrieved — no verdict is available either way


def near_miss_outcome(span_retrieved: bool, abstained: bool | None) -> str | None:
    """Pure. `abstained` is None when no abstention decision is wired, which is
    the case today: DECISIONS.md (2026-08-21) reserves the choice of signal —
    `rrf_score` cannot signal abstention and must be replaced by the reranker's
    score — for chunk 5. None is *not* folded into UNSCORED: "retrieval failed"
    and "nothing decided yet" are exactly the two things this must never
    conflate.
    """
    if not span_retrieved:
        return UNSCORED
    if abstained is None:
        return None
    return ABSTAINED if abstained else ANSWERED


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
    # --- near-miss abstention scoring ---
    # `near_miss_outcomes` values are None for a SCORED item while no abstention
    # decision is wired (see `near_miss_outcome`). n_scored/n_unscored are fields
    # rather than something the reader derives, so an abstention rate can never
    # be read off this report without the coverage it was fitted on.
    near_miss_retrieved: dict[str, bool]
    near_miss_outcomes: dict[str, str | None]
    n_scored: int
    n_unscored: int
    abstention_top_k: int


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
    abstained_by: Callable[[Retrieval], bool] | None = None,
) -> EvaluationReport:
    """Run all three retrieval arms over the golden set.

    `abstained_by` decides, per near-miss, whether the system abstained. It
    defaults to None — no decision — because the signal it would read is chunk
    5's to choose (DECISIONS.md 2026-08-21). With None, near-miss items are still
    marked SCORED/UNSCORED, which needs only retrieval; chunk 5 passes one
    argument and the three outcomes go live through this same code path.

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
    near_miss_retrieved: dict[str, bool] = {}
    near_miss_outcomes: dict[str, str | None] = {}
    for q in unanswerable:
        abstention = await retrieve(
            q.question,
            session,
            vector_store,
            embedder,
            top_k=ABSTENTION_TOP_K,
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

        # "Retrieved" is the answerable path's rule, unchanged and not
        # re-derived: `resolve_gold_chunk_ids` already mapped this entry's
        # `near_miss_to` span to its owning chunk by containment (it runs over
        # every entry, not just the answerable ones), and the test is chunk-id
        # membership — the same `set(...) & gold` form `_rank_metrics` uses.
        #
        # Against `hits`, i.e. the ABSTENTION_TOP_K the decision actually
        # consumed. Not recall@10, not the rerank_n=40 pool retrieve() built and
        # cut away: scoring against a set the decision never saw would credit
        # the abstention to evidence it never had.
        retrieved = bool({h.chunk_id for h in hits} & gold_by_id[q.id])
        near_miss_retrieved[q.id] = retrieved
        near_miss_outcomes[q.id] = near_miss_outcome(
            retrieved, None if abstained_by is None else abstained_by(abstention)
        )

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
        near_miss_retrieved=near_miss_retrieved,
        near_miss_outcomes=near_miss_outcomes,
        n_scored=sum(near_miss_retrieved.values()),
        n_unscored=sum(not ok for ok in near_miss_retrieved.values()),
        abstention_top_k=ABSTENTION_TOP_K,
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
    lines.extend(_abstention_block(report))
    return "\n".join(lines)


def _abstention_rate(report: EvaluationReport) -> str:
    """The rate line, which never renders as a bare number.

    It always carries its own denominator and the coverage it was fitted on, so
    it cannot be quoted without them. Two cases deliberately refuse to produce a
    number at all: no decision wired (there is no rate to compute), and zero
    scored items — `0/0` printed as `0.000` would read as "abstained on nothing"
    when the truth is "measured nothing".
    """
    n = report.n_unanswerable
    scored = [
        outcome
        for qid, outcome in report.near_miss_outcomes.items()
        if report.near_miss_retrieved[qid]
    ]
    if not scored:
        return f"n/a (0 scored of {n})"
    if any(outcome is None for outcome in scored):
        return f"n/a — no abstention decision wired; {report.n_scored} scored of {n}"
    correct = sum(outcome == ABSTAINED for outcome in scored)
    return (
        f"{correct}/{len(scored)} = {correct / len(scored):.3f} over SCORED items "
        f"(coverage {report.n_scored}/{n})"
    )


def _abstention_block(report: EvaluationReport) -> list[str]:
    """Coverage sits next to the rate, in the same printed block, always.

    A rate whose coverage is only recoverable from logs is a rate that gets
    quoted without it — which is the exact failure DECISIONS.md 2026-08-20
    Note 2 exists to prevent.
    """
    n = report.n_unanswerable
    unscored_ids = [qid for qid, ok in report.near_miss_retrieved.items() if not ok]
    coverage = (
        f"near-miss retrieval coverage: {report.n_scored}/{n} SCORED, {report.n_unscored} UNSCORED"
    )
    if unscored_ids:
        coverage += f" ({', '.join(unscored_ids)})"

    lines = [
        f"abstention over the {n} NEAR-MISS entries — SCORED only when the near_miss_to "
        f"span was in the set the abstention decision consumed "
        f"(top_k={report.abstention_top_k}, arm=hybrid, reranker={report.reranker}); "
        "never folded into recall or MRR:",
        coverage,
    ]
    if report.n_unscored:
        lines.append(
            f"WARNING: {report.n_unscored} of {n} near-misses UNSCORED — any rate here "
            f"is fitted on {report.n_scored} item(s), not {n}"
        )
    lines.append(f"abstention rate: {_abstention_rate(report)}")
    lines.append("")
    lines.append(f"  {'id':<5}{'span_retrieved':<16}{'outcome':<11}top-1 rrf_score")
    for qid, score in report.abstention_scores.items():
        retrieved = "yes" if report.near_miss_retrieved[qid] else "no"
        outcome = report.near_miss_outcomes[qid] or "—"
        lines.append(f"  {qid:<5}{retrieved:<16}{outcome:<11}{score:>10.5f}")
    return lines


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

    # --- near-miss outcome truth table ---
    # A span that was not retrieved is UNSCORED whatever the system did — that
    # is the requirement (DECISIONS.md 2026-08-20 Note 2), and the case a
    # `not retrieved` + `abstained` row would otherwise score as a pass.
    assert near_miss_outcome(True, True) == ABSTAINED
    assert near_miss_outcome(True, False) == ANSWERED
    assert near_miss_outcome(False, True) == UNSCORED
    assert near_miss_outcome(False, False) == UNSCORED
    assert near_miss_outcome(False, None) == UNSCORED
    # No decision wired stays distinct from UNSCORED: conflating "retrieval
    # failed" with "nothing decided yet" is the confound this whole check exists
    # to remove.
    assert near_miss_outcome(True, None) is None

    # --- rate and coverage rendering ---
    def _report(retrieved: dict[str, bool], outcomes: dict[str, str | None]) -> EvaluationReport:
        empty = ArmMetrics(recall_at=dict.fromkeys(RECALL_KS, 0.0), mrr=0.0, n_questions=0)
        return EvaluationReport(
            lexical=empty,
            vector=empty,
            hybrid=empty,
            abstention_scores=dict.fromkeys(retrieved, 0.0),
            rrf_k=RRF_K,
            candidate_k=DEFAULT_CANDIDATE_K,
            golden_set_path="-",
            golden_set_version=3,
            n_answerable=30,
            n_unanswerable=len(retrieved),
            reranker="NoOpReranker",
            mrr_depth=MRR_DEPTH,
            rerank_n=RERANK_N,
            near_miss_retrieved=retrieved,
            near_miss_outcomes=outcomes,
            n_scored=sum(retrieved.values()),
            n_unscored=sum(not ok for ok in retrieved.values()),
            abstention_top_k=ABSTENTION_TOP_K,
        )

    ids = [f"u{i:02d}" for i in range(1, 7)]

    # Zero coverage must never render a number: 0/0 shown as 0.000 reads as
    # "abstained on nothing" when the truth is "measured nothing".
    none_retrieved = _report(dict.fromkeys(ids, False), dict.fromkeys(ids, UNSCORED))
    block = "\n".join(_abstention_block(none_retrieved))
    rate_line = next(ln for ln in block.splitlines() if ln.startswith("abstention rate:"))
    assert rate_line == "abstention rate: n/a (0 scored of 6)", rate_line
    # Scoped to the rate line, not the whole block: the score column legitimately
    # contains 0.00000 here. It is the *rate* that must never render as a number.
    assert "0.000" not in rate_line, rate_line
    assert "coverage: 0/6 SCORED, 6 UNSCORED (u01, u02, u03, u04, u05, u06)" in block, block
    assert "WARNING: 6 of 6 near-misses UNSCORED" in block, block

    # Partial coverage with a decision wired: rate over SCORED only, and the
    # coverage it was fitted on printed in the same block, not derivable
    # elsewhere.
    partial = dict(zip(ids, [True, True, True, True, False, False], strict=True))
    outcomes: dict[str, str | None] = {
        "u01": ABSTAINED,
        "u02": ABSTAINED,
        "u03": ABSTAINED,
        "u04": ANSWERED,
        "u05": UNSCORED,
        "u06": UNSCORED,
    }
    block = "\n".join(_abstention_block(_report(partial, outcomes)))
    assert "abstention rate: 3/4 = 0.750 over SCORED items (coverage 4/6)" in block, block
    assert "coverage: 4/6 SCORED, 2 UNSCORED (u05, u06)" in block, block
    # The two UNSCORED items must not sit in either half of the rate: 3/4, never
    # 3/6 (which would dilute it) and never 5/6 (which would score them as pass).
    assert "3/6" not in block and "5/6" not in block, block

    # No decision wired — today's default. UNSCORED is still computed, because it
    # depends only on retrieval; the rate refuses.
    unwired: dict[str, str | None] = {
        **dict.fromkeys(ids[:4], None),
        **dict.fromkeys(ids[4:], UNSCORED),
    }
    block = "\n".join(_abstention_block(_report(partial, unwired)))
    assert "no abstention decision wired; 4 scored of 6" in block, block
    assert "UNSCORED" in block, block

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
        "ok: recall@k/MRR math, the near-miss outcome truth table and its coverage/rate "
        "rendering, and the cache round-trip offline; gold resolution reproduces all 13 of "
        "v2's recorded chunk_id sets, v3 resolves 30+6, and 5 broken spans all raise"
    )


if __name__ == "__main__":
    _demo()
