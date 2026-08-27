"""Chunk 7.1 — reranker score distributions. MEASUREMENT ONLY.

    uv run --no-sync python -m scripts.chunk7_scores

**Selects no threshold.** That is 7.2, and `docs/DECISIONS.md` (2026-08-23, "OPEN QUESTION carried
into chunk 7") makes this run its precondition:

    "Chunk 7 may not fit a Guardrail threshold on reranker score without first establishing how
     often the cross-encoder demotes a correct chunk, and by how much."

Nothing here proposes a cutoff, writes Guardrail code, or touches abstention logic.

Three populations, scored with the reranker active, on the **vector** (primary) and **hybrid** arms:

    (a) answerable gold hits      — the reranker score of the correct chunk
    (b) near-miss top-1           — u01..u06; u06 reported SEPARATELY, never in summary stats
    (c) non-gold hits, ranks 1-5  — everything in the reranked top-5 that is not gold

Lexical is excluded on purpose: its recall@30 is 0.567, so ~13 of 30 gold chunks never enter the
pool at all and population (a) would be n≈17 — a different population, not a comparable arm.

Cost: $0, no network. Query vectors come from the warm cache (`allow_network` stays False, so a
miss raises rather than spends). Only the reranked pass runs inference: 2 arms x 36 questions x 40
candidates, one `session.run()` per pair.

**Batch size 1 is mandatory and is not an efficiency oversight.** Logits are bitwise reproducible
only at batch size 1; batching perturbs a logit by ~0.1-0.42 (`reranking.py`, DECISIONS.md
2026-08-21). `LocalOnnxReranker` already scores one pair at a time — do not "optimise" that.

Scores are stored as RAW LOGITS. `reranking.RerankResult.scores` is explicit that a sigmoid belongs
at the presentation boundary (`to_probability()`) and never in storage, because a logit near ±10
saturates in float32 and destroys the resolution the tie-break depends on. A `to_probability`
mirror is emitted alongside, clearly marked display-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import _build_embedder, _build_reranker
from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.services.context_budget import ContextBudget, TiktokenCounter, plan_context
from app.services.embeddings import Embedder
from app.services.evaluation import (
    GOLDEN_SET_PATH,
    GateMismatch,
    GoldenEntry,
    assert_gate,
    evaluate,
    load_golden_set,
    load_query_vectors,
    resolve_target_chunk_ids,
)
from app.services.reranking import NOOP_RERANKER, RERANK_N, Reranker
from app.services.retrieval import DEFAULT_CANDIDATE_K, RetrievedChunk, retrieve
from app.services.vector_store import ChromaVectorStore, VectorStore

# Reused rather than reimplemented: one manifest convention for the repo, and a second copy is a
# second thing to drift. `_manifest` needs an EvaluationReport, which the gate pass below produces.
from scripts.chunk5_benchmark import _manifest

DEFAULT_OUT = Path("docs/chunk7-scores.json")

Arm = Literal["vector", "hybrid"]
ARMS_MEASURED: tuple[Arm, ...] = ("vector", "hybrid")
PRIMARY_ARM: Arm = "vector"

# The regression this run exists to locate, from docs/chunk5-results.json. Recomputed from the
# measured data below and compared — a mismatch is a finding, not something to quietly absorb.
EXPECTED_VECTOR_RECALL_AT_5 = {"pre_rerank": "0.833", "post_rerank": "0.733"}

TOP_K_DECISION = 5
"""The depth populations (c) and the recall@5 diff are read at. Not a threshold — a rank cut that
already exists in RECALL_KS."""

# u06's designated lure is not the lure retrieval produces, so u06 is UNSCORED in every arm and its
# abstention behaviour was never exercised (DECISIONS.md 2026-08-23, "KNOWN LIMITATION"). That entry
# also forbids pooling it with correct-answer demotions: doing so would feed a lure-miss into the
# calibration as if it were a relevance error.
U06_EXCLUSION_REASON = (
    "u06's designated near_miss_to lure (p95 agentic-chunking pricing) is not the chunk retrieval "
    "surfaces — all arms rank the p90 passage describing the named technique above it, so the lure "
    "never reaches rank 1 and u06 is UNSCORED in every configuration. Reported separately and "
    "excluded from population (b) summary statistics per docs/DECISIONS.md 2026-08-23 "
    "('KNOWN LIMITATION of golden set v3's near-miss set'), which states that pooling a lure-miss "
    "with correct-answer demotions would corrupt chunk 7's threshold calibration."
)

AGGREGATION_NOTE = (
    "Population (a) is aggregated max-per-question as the PRIMARY figure: one score per question, "
    "the best-scoring gold chunk. A question's golden-set span can resolve to more than one chunk "
    "(evaluation.resolve_target_chunk_ids returns a set, and _rank_metrics scores recall "
    "fractionally because of it), so a pooled-across-all-gold-chunks population has n >= "
    "n_questions and weights multi-gold questions more heavily. Both are reported, plus the "
    "within-question spread for every multi-gold question, so the choice is visible rather than "
    "buried in the aggregation."
)


def _summary(values: list[float]) -> dict[str, Any]:
    """min/max/median/quartiles, or nulls with a reason when n is too small to quantile."""
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "min": None,
            "q1": None,
            "median": None,
            "q3": None,
            "max": None,
            "reason": "empty population",
        }
    out: dict[str, Any] = {
        "n": n,
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }
    if n >= 4:
        q1, _, q3 = statistics.quantiles(values, n=4)
        out["q1"], out["q3"] = q1, q3
    else:
        out["q1"] = out["q3"] = None
        out["quartile_reason"] = f"n={n} too small for stable quartiles; see the full sorted list"
    return out


def _percentile_of(value: float, population: list[float]) -> float | None:
    """Fraction of `population` at or below `value`. Descriptive only — not a cutoff."""
    if not population:
        return None
    return sum(1 for v in population if v <= value) / len(population)


async def _both_passes(
    entry: GoldenEntry,
    query_vector: list[float],
    arm: Arm,
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    reranker: Reranker,
) -> tuple[list[RetrievedChunk], list[RetrievedChunk]]:
    """(post, pre) at full pool depth.

    `top_k=RERANK_N` is what makes one call enough: `LocalOnnxReranker` scores every candidate it is
    given and `_rank` cuts only `order`, so asking for the whole pool returns all 40 hits WITH their
    scores — including gold chunks that the reranker pushed below rank 5, which is exactly what
    item 4 needs and what a `top_k=5` call would have thrown away.

    The pre pass uses NOOP_RERANKER, which returns `scores={}` and therefore leaves every
    `rerank_score` None — the same property that makes `score_source` read "rrf" there.
    """
    common = dict(
        session=session,
        vector_store=vector_store,
        embedder=embedder,
        top_k=RERANK_N,
        candidate_k=DEFAULT_CANDIDATE_K,
        query_vector=query_vector,
        arm=arm,
        rerank_n=RERANK_N,
    )
    post = await retrieve(entry.question, reranker=reranker, **common)  # type: ignore[arg-type]
    pre = await retrieve(entry.question, reranker=NOOP_RERANKER, **common)  # type: ignore[arg-type]
    return post.hits, pre.hits


def _populations(
    arm_data: dict[str, dict[str, Any]],
    answerable: list[GoldenEntry],
    near_miss: list[GoldenEntry],
    gold_by_id: dict[str, set[str]],
) -> dict[str, Any]:
    """Populations (a), (b), (c) for one arm, from the already-retrieved passes."""
    # --- (a) answerable gold hits ---
    per_question: list[dict[str, Any]] = []
    pooled: list[float] = []
    max_per_question: list[float] = []
    spreads: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []

    for entry in answerable:
        post_by_id = arm_data[entry.id]["post_by_id"]
        gold = gold_by_id[entry.id]
        found = {cid: post_by_id[cid].rerank_score for cid in sorted(gold) if cid in post_by_id}
        missing = sorted(gold - set(post_by_id))
        for cid in missing:
            # Never silently dropped: a gold chunk outside the 40-deep pool has no reranker score
            # to report, and saying so is different from saying it scored low.
            absent.append(
                {
                    "question_id": entry.id,
                    "chunk_id": cid,
                    "rerank_score": None,
                    "reason": f"gold chunk not in the {RERANK_N}-deep retrieval pool; never scored",
                }
            )
        scores = list(found.values())
        pooled.extend(scores)
        record: dict[str, Any] = {
            "question_id": entry.id,
            "n_gold_chunks": len(gold),
            "n_gold_scored": len(scores),
            "scores": {cid: s for cid, s in found.items()},
        }
        if scores:
            best = max(scores)
            max_per_question.append(best)
            record["max"] = best
            record["post_rerank_rank_of_best"] = min(
                post_by_id[cid].rerank_rank for cid, s in found.items() if s == best
            )
            if len(scores) > 1:
                record["spread"] = max(scores) - min(scores)
                spreads.append(
                    {
                        "question_id": entry.id,
                        "n_gold_scored": len(scores),
                        "min": min(scores),
                        "max": best,
                        "spread": max(scores) - min(scores),
                    }
                )
        else:
            record["max"] = None
        per_question.append(record)

    # --- (b) near-miss top-1, u06 held out ---
    nm_scored: list[dict[str, Any]] = []
    u06_row: dict[str, Any] | None = None
    for entry in near_miss:
        hits = arm_data[entry.id]["post"]
        top = hits[0] if hits else None
        row = {
            "question_id": entry.id,
            "chunk_id": top.chunk_id if top else None,
            "rerank_score": top.rerank_score if top else None,
            "top1_is_designated_lure": bool(top and top.chunk_id in gold_by_id[entry.id]),
        }
        if entry.id == "u06":
            row["excluded_from_summary"] = True
            row["exclusion_reason"] = U06_EXCLUSION_REASON
            u06_row = row
        else:
            nm_scored.append(row)
    nm_values = [r["rerank_score"] for r in nm_scored if r["rerank_score"] is not None]

    # --- (c) non-gold hits at post-rerank ranks 1-5 ---
    non_gold: list[float] = []
    non_gold_detail: list[dict[str, Any]] = []
    for entry in answerable:
        gold = gold_by_id[entry.id]
        for hit in arm_data[entry.id]["post"][:TOP_K_DECISION]:
            if hit.chunk_id not in gold:
                non_gold.append(hit.rerank_score)
                non_gold_detail.append(
                    {
                        "question_id": entry.id,
                        "chunk_id": hit.chunk_id,
                        "post_rerank_rank": hit.rerank_rank,
                        "rerank_score": hit.rerank_score,
                    }
                )

    return {
        "a_answerable_gold": {
            "aggregation": "max_per_question (PRIMARY); pooled and spread reported alongside",
            "primary_max_per_question": _summary(sorted(max_per_question)),
            "pooled_all_gold_chunks": _summary(sorted(pooled)),
            "within_question_spread": {
                "n_multi_gold_questions": len(spreads),
                "questions": sorted(spreads, key=lambda s: -s["spread"]),
            },
            "gold_chunks_absent_from_pool": absent,
            "per_question": per_question,
        },
        "b_near_miss_top1": {
            "scored": _summary(sorted(nm_values)),
            "full_sorted_list": sorted(nm_scored, key=lambda r: r["rerank_score"] or 0.0),
            "u06_reported_separately": u06_row,
        },
        "c_non_gold_top5": {
            **_summary(sorted(non_gold)),
            "depth": TOP_K_DECISION,
            "rank_basis": "post-rerank — what a Guardrail would see at decision time",
        },
        "_values": {  # internal, stripped before writing
            "a_max": max_per_question,
            "a_pooled": pooled,
            "b": nm_values,
            "c": non_gold,
            "c_detail": non_gold_detail,
        },
    }


def _overlap(a: list[float], b: list[float], c: list[float]) -> dict[str, Any]:
    """Where a positive and a negative population share a score range.

    Reported as an interval and a count, with no recommendation attached — the question this answers
    is "could a hit at this score be either?", not "where should a line go?".
    """

    def region(pos: list[float], neg: list[float], neg_name: str) -> dict[str, Any]:
        if not pos or not neg:
            return {"overlaps": False, "reason": "one population is empty"}
        lo, hi = max(min(pos), min(neg)), min(max(pos), max(neg))
        if lo > hi:
            return {
                "overlaps": False,
                "separable": True,
                "gap": min(pos) - max(neg),
                "note": f"gold minimum sits entirely above the {neg_name} maximum",
            }
        return {
            "overlaps": True,
            "region": [lo, hi],
            "n_gold_inside": sum(1 for v in pos if lo <= v <= hi),
            "n_gold_total": len(pos),
            "n_negative_inside": sum(1 for v in neg if lo <= v <= hi),
            "n_negative_total": len(neg),
        }

    negatives: dict[str, Any] = {}
    if b and c:
        lo, hi = max(min(b), min(c)), min(max(b), max(c))
        negatives = {
            "b_range": [min(b), max(b)],
            "c_range": [min(c), max(c)],
            "overlaps": lo <= hi,
            "region": [lo, hi] if lo <= hi else None,
            "b_median": statistics.median(b),
            "c_median": statistics.median(c),
        }

    return {
        "a_vs_b": region(a, b, "near-miss top-1"),
        "a_vs_c": region(a, c, "non-gold top-5"),
        "b_vs_c_are_the_negatives_distinguishable": negatives,
    }


def _recall_at_5_regression(
    arm_data: dict[str, dict[str, Any]],
    answerable: list[GoldenEntry],
    gold_by_id: dict[str, set[str]],
    pop_values: dict[str, list[float]],
) -> dict[str, Any]:
    """Item 4 — locate the demotions behind vector recall@5 0.833 -> 0.733.

    The per-question membership diff is DERIVED, never inferred from the aggregate delta. That
    inference is not merely imprecise here, it is wrong: the measured vector arm has FIVE questions
    losing a gold chunk and TWO gaining one, netting the -3/30 the -0.100 delta encodes. Reading
    "3 questions" off the delta names the wrong count and hides both directions of movement.

    Improvements are therefore reported beside losses — a net figure that conceals offsetting
    movement is exactly what this function exists to take apart.
    """
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    pre_recalls: list[float] = []
    post_recalls: list[float] = []

    for entry in answerable:
        gold = gold_by_id[entry.id]
        post_hits = arm_data[entry.id]["post"]
        pre_hits = arm_data[entry.id]["pre"]
        post_by_id = arm_data[entry.id]["post_by_id"]
        pre_rank = {h.chunk_id: i + 1 for i, h in enumerate(pre_hits)}

        pre_top5 = [h.chunk_id for h in pre_hits[:TOP_K_DECISION]]
        post_top5 = [h.chunk_id for h in post_hits[:TOP_K_DECISION]]
        pre_gold, post_gold = set(pre_top5) & gold, set(post_top5) & gold

        pre_r, post_r = len(pre_gold) / len(gold), len(post_gold) / len(gold)
        pre_recalls.append(pre_r)
        post_recalls.append(post_r)

        gained = sorted(post_gold - pre_gold)
        if gained:
            improvements.append(
                {
                    "question_id": entry.id,
                    "n_gold_chunks": len(gold),
                    "n_gold_gained_into_top5": len(gained),
                    "recall_at_5_pre": pre_r,
                    "recall_at_5_post": post_r,
                    "promoted_gold": [
                        {
                            "chunk_id": cid,
                            "rerank_score": post_by_id[cid].rerank_score,
                            "pre_rerank_rank": pre_rank.get(cid),
                            "post_rerank_rank": post_by_id[cid].rerank_rank,
                        }
                        for cid in gained
                    ],
                }
            )

        lost = sorted(pre_gold - post_gold)
        if not lost:
            continue

        displacers = [
            {
                "chunk_id": h.chunk_id,
                "post_rerank_rank": h.rerank_rank,
                "rerank_score": h.rerank_score,
                "is_gold": h.chunk_id in gold,
                "pre_rerank_rank": pre_rank.get(h.chunk_id),
                "percentile_in_c_non_gold": _percentile_of(h.rerank_score, pop_values["c"]),
                "percentile_in_a_gold": _percentile_of(h.rerank_score, pop_values["a_max"]),
            }
            for h in post_hits[:TOP_K_DECISION]
            if h.chunk_id not in pre_top5
        ]

        regressions.append(
            {
                "question_id": entry.id,
                "n_gold_chunks": len(gold),
                "n_gold_lost_from_top5": len(lost),
                "recall_at_5_pre": pre_r,
                "recall_at_5_post": post_r,
                "demoted_gold": [
                    {
                        "chunk_id": cid,
                        "rerank_score": post_by_id[cid].rerank_score if cid in post_by_id else None,
                        "pre_rerank_rank": pre_rank.get(cid),
                        "post_rerank_rank": post_by_id[cid].rerank_rank
                        if cid in post_by_id
                        else None,
                        "percentile_in_a_gold": _percentile_of(
                            post_by_id[cid].rerank_score, pop_values["a_max"]
                        )
                        if cid in post_by_id
                        else None,
                        "percentile_in_c_non_gold": _percentile_of(
                            post_by_id[cid].rerank_score, pop_values["c"]
                        )
                        if cid in post_by_id
                        else None,
                    }
                    for cid in lost
                ],
                "displaced_into_top5_by": displacers,
            }
        )

    mean_pre = sum(pre_recalls) / len(pre_recalls)
    mean_post = sum(post_recalls) / len(post_recalls)
    n_lost = sum(r["n_gold_lost_from_top5"] for r in regressions)
    n_gained = sum(i["n_gold_gained_into_top5"] for i in improvements)
    return {
        "measured_recall_at_5": {
            "pre_rerank": f"{mean_pre:.3f}",
            "post_rerank": f"{mean_post:.3f}",
            "delta": f"{mean_post - mean_pre:+.3f}",
        },
        "n_questions_regressed": len(regressions),
        "n_gold_chunks_lost_total": n_lost,
        "n_questions_improved": len(improvements),
        "n_gold_chunks_gained_total": n_gained,
        "net_gold_chunks": n_gained - n_lost,
        "derivation_note": (
            f"Measured directly from the top-5 membership diff: {len(regressions)} question(s) "
            f"lost {n_lost} gold chunk(s), {len(improvements)} question(s) gained {n_gained}, "
            f"netting {n_gained - n_lost}. The aggregate delta encodes only the net, so inferring "
            "a question count from it names the wrong number and hides the offsetting movement. "
            "Fractional recall over multi-chunk gold sets makes that inference ambiguous in "
            "general; here it is simply wrong."
        ),
        "questions_regressed": regressions,
        "questions_improved": improvements,
    }


def _stop_condition(
    regression: dict[str, Any],
    pop_values: dict[str, list[float]],
    overlap: dict[str, Any],
) -> dict[str, Any]:
    """Does the reranker score separate grounded from ungrounded? BLOCKING question for 7.2.

    docs/DECISIONS.md (2026-08-23) makes this run 7.2's precondition and names the risk:
    "A cross-encoder that confidently demotes correct chunks is scoring them low, and a threshold
    fitted on those scores will inherit the error directly."

    Separation can fail in EITHER direction, and both are tested — an earlier version of this check
    tested only the HIGH one and reported a clean result while the data said otherwise:

      LOW  — a demoted gold chunk scoring below the non-gold median: the correct chunk looks worse
             than a typical wrong one, so a threshold admitting typical wrong chunks would reject
             it, and the system abstains on questions whose evidence it actually retrieved.
      HIGH — a near-miss lure (or a displacing non-gold chunk) scoring above the gold median: the
             wrong-but-plausible chunk looks better than a typical correct one, so a threshold
             admits lures confidently.

    Medians are descriptive reference points for saying where a value sits. They are NOT proposed
    cutoffs, and nothing here selects one.
    """
    gold, near_miss, non_gold = pop_values["a_max"], pop_values["b"], pop_values["c"]
    gold_median = statistics.median(gold) if gold else None
    c_median = statistics.median(non_gold) if non_gold else None

    demoted = [
        d
        for q in regression["questions_regressed"]
        for d in q["demoted_gold"]
        if d["rerank_score"] is not None
    ]
    displacers = [
        d
        for q in regression["questions_regressed"]
        for d in q["displaced_into_top5_by"]
        if d["rerank_score"] is not None and not d["is_gold"]
    ]
    demoted_below_c_median = [
        d for d in demoted if c_median is not None and d["rerank_score"] < c_median
    ]
    near_miss_above_gold_median = [
        v for v in near_miss if gold_median is not None and v > gold_median
    ]
    displacers_above_gold_median = [
        d for d in displacers if gold_median is not None and d["rerank_score"] > gold_median
    ]

    a_vs_c = overlap.get("a_vs_c", {})
    overlap_fraction = None
    if a_vs_c.get("overlaps"):
        overlap_fraction = {
            "score_region": a_vs_c["region"],
            "gold_inside_overlap": f"{a_vs_c['n_gold_inside']}/{a_vs_c['n_gold_total']}",
            "non_gold_inside_overlap": (
                f"{a_vs_c['n_negative_inside']}/{a_vs_c['n_negative_total']}"
            ),
        }

    low_fires = bool(demoted_below_c_median)
    high_fires = bool(near_miss_above_gold_median) or bool(displacers_above_gold_median)

    return {
        "blocking_on": "7.2 threshold selection (docs/DECISIONS.md 2026-08-23, OPEN QUESTION)",
        "triggered": low_fires or high_fires,
        "directions_fired": (
            [n for n, f in (("low", low_fires), ("high", high_fires)) if f] or ["none"]
        ),
        "reference_medians": {
            "population_a_gold_max_per_question": gold_median,
            "population_c_non_gold_top5": c_median,
            "note": "descriptive reference points, NOT proposed cutoffs",
        },
        "low_direction": {
            "description": (
                "demoted gold chunks scoring below the non-gold median — the correct chunk looks "
                "worse than a typical wrong one"
            ),
            "n_demoted_gold_below_non_gold_median": len(demoted_below_c_median),
            "n_demoted_gold_total": len(demoted),
            "detail": [
                {
                    "question_id": q["question_id"],
                    "chunk_id": d["chunk_id"],
                    "rerank_score": d["rerank_score"],
                    "percentile_in_c_non_gold": d["percentile_in_c_non_gold"],
                }
                for q in regression["questions_regressed"]
                for d in q["demoted_gold"]
                if d["rerank_score"] is not None
            ],
        },
        "high_direction": {
            "description": (
                "near-miss lures / displacing non-gold chunks scoring above the gold median — the "
                "wrong-but-plausible chunk looks better than a typical correct one"
            ),
            "n_near_miss_above_gold_median": len(near_miss_above_gold_median),
            "n_near_miss_total": len(near_miss),
            "near_miss_scores_above_gold_median": sorted(near_miss_above_gold_median),
            "n_displacers_above_gold_median": len(displacers_above_gold_median),
        },
        "population_overlap": overlap_fraction,
        "reading": (
            "Reported as measurement. 7.2 owns what to do about it and NO cutoff is proposed here. "
            "What is measured is whether a single scalar reranker score can separate grounded from "
            "ungrounded evidence on this corpus at all."
        ),
    }


def _score_source_check(
    arm_data: dict[str, dict[str, Any]], counter: TiktokenCounter
) -> dict[str, Any]:
    """Item 5 — the reranked path must read "rerank" uniformly, the NoOp path "rrf".

    `plan_context` is the public surface that decides this, so it is what gets called; the counter
    is passed explicitly rather than relying on the default, so a measurement run does not become
    the first real exercise of the dormant TiktokenCounter default path.

    `MixedScoreSources` is deliberately NOT caught. If it ever fires, some hits in one retrieval
    carry a reranker score and others do not — that is a finding about the retrieval path, not an
    error to route around.
    """
    budget = ContextBudget()
    post_sources, pre_sources = set(), set()
    for data in arm_data.values():
        post_sources.add(plan_context(data["post"], budget, counter).score_source)
        pre_sources.add(plan_context(data["pre"], budget, counter).score_source)

    assert post_sources == {"rerank"}, f"reranked path was not uniformly 'rerank': {post_sources}"
    assert pre_sources == {"rrf"}, f"NoOp path was not uniformly 'rrf': {pre_sources}"

    return {
        "reranked_path": sorted(post_sources),
        "uniform_rerank": post_sources == {"rerank"},
        "noop_path": sorted(pre_sources),
        "uniform_rrf": pre_sources == {"rrf"},
        "mixed_score_sources_raised": False,
        "n_retrievals_checked": len(arm_data) * 2,
        "note": (
            "The two signals are distinguishable from the result object alone — NoOpReranker "
            "returns scores={}, so every hit keeps rerank_score=None and _score_source reads "
            "'rrf'. "
            "That is the signal the Guardrail must refuse to calibrate on. 7.1 confirms only that "
            "it is distinguishable; it wires no refusal."
        ),
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chunk7_scores", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    settings = get_settings()
    embedder = _build_embedder(settings)
    engine = create_engine(settings)
    vector_store = ChromaVectorStore(settings.chroma_persist_dir)
    session_factory = create_session_factory(engine)
    reranker: Reranker | None = None

    try:
        async with session_factory() as session:
            # --- gate first: a failed gate must not even load the model ------------------
            print("pass 1/2 — regression gate (--reranker none)...", file=sys.stderr)
            gate_report = await evaluate(session, vector_store, embedder, reranker=NOOP_RERANKER)
            try:
                assert_gate(gate_report)
            except GateMismatch as exc:
                print(f"\nGATE FAILED — no scores were measured.\n{exc}", file=sys.stderr)
                return 1
            print("gate PASS — all 12 no-rerank figures reproduce exactly.\n", file=sys.stderr)

            reranker = _build_reranker(settings, "local")
            counter = TiktokenCounter()

            entries = load_golden_set(GOLDEN_SET_PATH)
            answerable = [e for e in entries if e.answerable]
            near_miss = [e for e in entries if not e.answerable]
            gold_by_id = await resolve_target_chunk_ids(session, entries)
            # allow_network defaults to False: a cache miss raises rather than spends.
            vectors = await load_query_vectors({e.id: e.question for e in entries}, embedder)

            by_arm: dict[str, Any] = {}
            for arm in ARMS_MEASURED:
                print(
                    f"pass 2/2 — scoring {arm} arm ({len(entries)} questions)...",
                    file=sys.stderr,
                )
                arm_data: dict[str, dict[str, Any]] = {}
                for entry in entries:
                    post, pre = await _both_passes(
                        entry, vectors[entry.id], arm, session, vector_store, embedder, reranker
                    )
                    arm_data[entry.id] = {
                        "post": post,
                        "pre": pre,
                        "post_by_id": {h.chunk_id: h for h in post},
                    }
                by_arm[arm] = arm_data

            document_id = answerable[0].document_id
            manifest = await _manifest(
                session, vector_store, embedder, settings, gate_report, document_id
            )

        # --- analysis (no more I/O) ---------------------------------------------------
        results_by_arm: dict[str, Any] = {}
        for arm in ARMS_MEASURED:
            pops = _populations(by_arm[arm], answerable, near_miss, gold_by_id)
            values = pops.pop("_values")
            regression = _recall_at_5_regression(by_arm[arm], answerable, gold_by_id, values)
            overlap = _overlap(values["a_max"], values["b"], values["c"])
            block: dict[str, Any] = {
                "populations": pops,
                "overlap": overlap,
                "recall_at_5_regression": regression,
                "stop_condition_for_7_2": _stop_condition(regression, values, overlap),
                "score_source": _score_source_check(by_arm[arm], counter),
            }
            if arm == PRIMARY_ARM:
                measured = regression["measured_recall_at_5"]
                block["recall_at_5_regression"]["expected_from_chunk5_results"] = (
                    EXPECTED_VECTOR_RECALL_AT_5
                )
                block["recall_at_5_regression"]["reproduces_chunk5"] = (
                    measured["pre_rerank"] == EXPECTED_VECTOR_RECALL_AT_5["pre_rerank"]
                    and measured["post_rerank"] == EXPECTED_VECTOR_RECALL_AT_5["post_rerank"]
                )
            results_by_arm[arm] = block

        manifest["measurement_choices"] = {
            "arms_measured": list(ARMS_MEASURED),
            "primary_arm": PRIMARY_ARM,
            "arms_excluded": {
                "lexical": (
                    "recall@30 is 0.567, so ~13 of 30 gold chunks never enter the rerank pool and "
                    "population (a) would be n≈17 — a different population, not a comparable arm."
                )
            },
            "population_a_aggregation": "max_per_question",
            "population_a_aggregation_note": AGGREGATION_NOTE,
            "population_c_rank_basis": "post-rerank ranks 1-5",
            "score_storage": (
                "raw cross-encoder logits, unbounded ~±10. to_probability() mirrors are "
                "display-only; reranking.py requires conversion at the presentation boundary, "
                "never in storage."
            ),
            "u06_handling": U06_EXCLUSION_REASON,
            "batch_size": 1,
            "batch_size_note": (
                "Logits are bitwise reproducible only at batch size 1; batching perturbs a "
                "logit by ~0.1-0.42. Not an efficiency oversight."
            ),
            "pool_nondeterminism": (
                "KNOWN, and upstream of the reranker: the Chroma vector pool's 30th "
                "(candidate_k boundary) result is not stable across processes -- it "
                "alternates between two chunk ids on at least one question (a03), with or "
                "without a reranker built. Verified by re-running this whole script: every "
                "headline figure is stable (recall@5 0.833 -> 0.733, 5 lost / 2 gained / net "
                "-3, the regressed question ids, both stop-condition directions, and all "
                "population summaries). Only a03's post-rerank rank (7 vs 8) and one entry of "
                "its displacer list move. Treat a03's displacer list below as ONE SAMPLE of "
                "two possible orderings, not a reproducible fact; the other four demotions "
                "are bit-stable."
            ),
        }

        results = {
            "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "step": "03",
            "chunk": "7.1",
            "scope": {
                "is": "measurement of reranker score distributions",
                "is_not": (
                    "threshold selection, Guardrail code, or any change to abstention logic — that "
                    "is 7.2 and depends on reading this output first"
                ),
                "precondition_for": (
                    "docs/DECISIONS.md 2026-08-23, OPEN QUESTION carried into chunk 7"
                ),
            },
            "manifest": manifest,
            "config": {
                "golden_set_path": gate_report.golden_set_path,
                "golden_set_version": gate_report.golden_set_version,
                "n_answerable": gate_report.n_answerable,
                "n_unanswerable": gate_report.n_unanswerable,
                "rrf_k": gate_report.rrf_k,
                "candidate_k": gate_report.candidate_k,
                "rerank_n": gate_report.rerank_n,
                "pool_depth_scored": RERANK_N,
                "decision_depth": TOP_K_DECISION,
            },
            "gate": {
                "reranker": gate_report.reranker,
                "status": "PASS",
                "note": "12/12 no-rerank figures reproduced before any score was measured",
            },
            "arms": results_by_arm,
        }

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nwrote {args.out}", file=sys.stderr)
        _print_summary(results)
        return 0
    finally:
        if reranker is not None:
            reranker.close()
        vector_store.close()
        await engine.dispose()


def _print_summary(results: dict[str, Any]) -> None:
    for arm, block in results["arms"].items():
        pops = block["populations"]
        reg = block["recall_at_5_regression"]
        stop = block["stop_condition_for_7_2"]
        print(f"\n=== {arm} ===")
        for label, summary in (
            ("(a) gold, max/question", pops["a_answerable_gold"]["primary_max_per_question"]),
            ("(b) near-miss top-1   ", pops["b_near_miss_top1"]["scored"]),
            ("(c) non-gold top-5    ", pops["c_non_gold_top5"]),
        ):
            q1 = f"{summary['q1']:.3f}" if summary.get("q1") is not None else "  n/a"
            q3 = f"{summary['q3']:.3f}" if summary.get("q3") is not None else "  n/a"
            print(
                f"  {label}  n={summary['n']:>3}  min={summary['min']:>7.3f}  q1={q1:>7}  "
                f"med={summary['median']:>7.3f}  q3={q3:>7}  max={summary['max']:>7.3f}"
            )
        m = reg["measured_recall_at_5"]
        print(
            f"  recall@5 {m['pre_rerank']} -> {m['post_rerank']} ({m['delta']}); "
            f"{reg['n_questions_regressed']} question(s) LOST {reg['n_gold_chunks_lost_total']} "
            f"gold chunk(s), {reg['n_questions_improved']} GAINED "
            f"{reg['n_gold_chunks_gained_total']} (net {reg['net_gold_chunks']:+d})"
        )
        print(
            f"  STOP CONDITION for 7.2: triggered={stop['triggered']} "
            f"direction(s)={','.join(stop['directions_fired'])}"
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
