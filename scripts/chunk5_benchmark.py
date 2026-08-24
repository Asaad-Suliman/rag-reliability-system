"""Step 03 · chunk 5 — the DoD #5 benchmark driver. Read-only; issues SELECTs only.

    uv run python -m scripts.chunk5_benchmark [--out docs/chunk5-results.json]

Run as a module, not as a path: this app is not installed (`package = false`),
so `python scripts/…py` puts `scripts/` on `sys.path` instead of the repo root
and `import app` fails. `scripts/verify_golden_set.py` documents the path form
and has the same problem — that is pre-existing and untouched here.

Two passes, in this order and no other:

1. **Gate.** `evaluate()` with `NoOpReranker`, asserted figure-for-figure against
   `evaluation.GATE_FIGURES`. A mismatch aborts here — the reranker is not even
   constructed, let alone run. The gate exists to prove the corpus and the index
   did not move under the benchmark; the corpus is do-not-re-embed and
   unrebuildable, so a moved figure is drift, never a threshold to re-tune.
2. **Answerable, reranker ON.** The same three arms, the same 4-tuple.

Abstention is reported as ONE collapsed row, not per arm. At
`ABSTENTION_TOP_K = 1` a per-arm comparison is structurally unavailable: without
the reranker the arms score near-disjoint near-miss sets, and with it all three
return an identical top-1, making their rates equal by construction rather than
by measurement (09_Memory/DECISIONS.md, 2026-08-23). The u05 evidence below shows
that convergence happens regardless of whether the shared top-1 is correct -- on
u05 the arms converge onto a LURE.

Everything printed is also written to the results JSON, evidence included, so no
figure in the write-up rests on prose alone.

Cost: $0. `allow_network` is never passed, so a query-embedding cache miss raises
rather than spending. The only model that runs is the local pinned int8 ONNX
cross-encoder.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import _build_embedder, _build_reranker
from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.services.context_budget import (
    ContextBudget,
    HeuristicCharCounter,
    budget_manifest,
)
from app.services.embeddings import Embedder
from app.services.evaluation import (
    ARMS,
    GATE_FIGURES,
    GOLDEN_SET_PATH,
    EvaluationReport,
    GateMismatch,
    arm_tuple,
    assert_gate,
    evaluate,
    load_golden_set,
    load_query_vectors,
    resolve_gold_chunk_ids,
)
from app.services.ingestion import collection_name_for, sha256_file
from app.services.reranking import NOOP_RERANKER, RERANK_N, Reranker
from app.services.retrieval import DEFAULT_CANDIDATE_K, retrieve
from app.services.vector_store import ChromaVectorStore, VectorStore

DEFAULT_OUT = Path("docs/chunk5-results.json")
METRIC_LABELS = ("recall@5", "recall@10", "recall@30", "mrr@10")

# The trajectory this run exists to record, and the arm order it is stated in.
# These track u05's LURE chunk (u05 is a near-miss unanswerable), not a gold
# chunk. Recorded 2026-08-22; asserted, not adjusted. If the observed ranks differ, the
# run reports both and exits non-zero — a moved rank means the retrieval order
# changed, which is a finding, not a number to re-fit.
# Why coverage is 5/6 and not 6/6. Diagnosed 2026-08-23 by direct inspection, not
# inferred.
#
# u06 is a near-miss UNANSWERABLE, so its `near_miss_to` chunk is the LURE -- the
# passage the system is supposed to be tempted to answer from -- not a correct
# answer. The lure (chk_01M0D4BMGPC4TSS179JBQE32HB, p95, the agentic-chunking
# pricing passage) never reaches rank 1: every arm ranks
# chk_01M0D4BMGPTW90AQ81EM4R32VW (p90, the passage describing the technique the
# question names) first. Observed lure ranks -- lexical: not in pool; vector
# 2 -> 6; hybrid 8 -> 6. Nothing filtered it out.
#
# At ABSTENTION_TOP_K = 1 the decision reads only hits[0], so the lure was never
# put in front of it. u06's abstention behaviour is therefore UNTESTED, not
# passed -- which is exactly what UNSCORED means.
#
# This is NOT the cross-encoder demotion recorded as chunk 7's open question:
# that is a correct-answer demotion on the ANSWERABLE set, this is a lure that
# failed to fire on the UNANSWERABLE set. Pooling them would feed chunk 7's
# abstention threshold a lure-miss as if it were a relevance error. The fixture
# side of this is logged separately as a known limitation of v3's near-miss set
# (DECISIONS.md 2026-08-23); v3 stays frozen.
UNSCORED_REASON = (
    "u06 is UNSCORED in every arm: its designated lure never reached rank 1, so u06's "
    "abstention behaviour was never exercised -- untested, not passed. u06 is a near-miss "
    "unanswerable, so its near_miss_to chunk is the LURE, not a correct answer. That lure (p95, "
    "agentic-chunking pricing) is absent from lexical's pool entirely and sits at rank 2 -> 6 "
    "(vector) and 8 -> 6 (hybrid), while all three arms rank the p90 chunk describing the "
    "technique the question names at 1. Nothing filtered it out; at ABSTENTION_TOP_K = 1 the "
    "decision reads only hits[0]. NOT an instance of the cross-encoder demoting a correct chunk "
    "-- that is a separate finding on the answerable set. The fixture side is logged as a known "
    "limitation of golden set v3's near-miss set (DECISIONS.md 2026-08-23); v3 stays frozen."
)

U05_EXPECTED_PRE_RERANK = {"lexical": 12, "vector": 17, "hybrid": 9}
U05_EXPECTED_POST_RERANK = {"lexical": 1, "vector": 1, "hybrid": 1}


def _git_state() -> dict[str, Any]:
    """HEAD plus whether the tree was dirty when the run happened.

    The SHA alone would be a lie on a dirty tree — chunk 5's own code was
    uncommitted when these figures were produced, so a reader given only
    `a8db838` would reproduce a run without the gate in it. Dirty files are
    listed, not just flagged.
    """

    def _git(*argv: str) -> str:
        # NOT stripped: `status --porcelain` encodes the status in the first two
        # columns, so stripping the output would eat the leading space of the
        # first line and shift that one path by a character.
        return subprocess.run(["git", *argv], capture_output=True, text=True, check=True).stdout

    try:
        dirty = [ln[3:] for ln in _git("status", "--porcelain").splitlines() if ln]
        return {
            "head": _git("rev-parse", "HEAD").strip(),
            "dirty": bool(dirty),
            "dirty_files": dirty,
        }
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # Recorded, never guessed: "unknown" is diagnosable, a missing key is not.
        return {"head": None, "dirty": None, "error": str(exc)}


def _reranker_revision(manifest_path: Path) -> dict[str, str | None]:
    """Model name and upstream revision from the tracked checksum manifest.

    The manifest is what `build_reranker` verifies the weights against, so this
    names the exact weights that ran rather than a config value that only says
    which directory to look in.
    """
    model = revision = None
    for line in manifest_path.read_text().splitlines():
        if line.startswith("# revision:"):
            revision = line.split(":", 1)[1].strip()
        elif line.startswith("#") and model is None:
            model = line.lstrip("# ").strip()
    return {"model": model, "revision": revision}


async def _manifest(
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    settings: Any,
    report: EvaluationReport,
    document_id: str,
) -> dict[str, Any]:
    """What the run was fed, recorded beside what it produced.

    The gate exists to catch corpus drift. Without this block a future gate
    failure cannot be told apart from a fixture swap or a code change — the
    figures would move either way and the file would not say which. Corpus
    identity, fixture hash, weights revision and tree state are the four things
    that distinguish them.
    """
    chunk_count = (
        await session.execute(
            text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": document_id}
        )
    ).scalar_one()
    collection = collection_name_for(embedder)
    golden_path = Path(report.golden_set_path)

    return {
        "corpus": {
            "document_id": document_id,
            "chunk_count": chunk_count,
            "vector_count": await vector_store.count_by_document(collection, document_id),
            "chroma_collection": collection,
        },
        "golden_set": {
            "path": report.golden_set_path,
            "version": report.golden_set_version,
            "sha256": sha256_file(golden_path),
        },
        "embedder": {"model": embedder.model, "dimensions": embedder.dimensions},
        # The active counter's identity and all five reserve values, beside the
        # corpus id and the weights revision: a budgeted run has to be
        # reconstructible from the manifest alone, and two runs sharing a corpus
        # and a reranker but not a counter are not comparable.
        "budget": budget_manifest(ContextBudget(), HeuristicCharCounter()),
        "reranker": {
            "pass_1_gate": "none",
            "pass_2_answerable": {
                "backend": "local",
                **_reranker_revision(settings.reranker_manifest_path),
            },
        },
        "git": _git_state(),
    }


def _tuples(report: EvaluationReport) -> dict[str, list[str]]:
    """Per arm, in the harness's own order and precision. `arm_tuple` is the only
    reader of `ArmMetrics`, so the gate and this file cannot drift apart."""
    return {
        "lexical": list(arm_tuple(report.lexical)),
        "vector": list(arm_tuple(report.vector)),
        "hybrid": list(arm_tuple(report.hybrid)),
    }


async def _pool(
    question: str,
    query_vector: list[float],
    arm: Literal["lexical", "vector", "hybrid"],
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    reranker: Reranker = NOOP_RERANKER,
) -> list[str]:
    """The chunk ids `retrieve()` built, in order, at full pool depth.

    ponytail: the pre-rerank pool is read by running `retrieve()` with the NoOp
    reranker at `top_k = rerank_n` rather than by exposing `retrieve()`'s
    internal candidate list. `pool_size = max(rerank_n, top_k)` and NoOp passes
    order through untouched, so this IS that list — with the advantage that it
    comes through the same truncation and provenance code the real path uses.
    Upgrade path if a second consumer ever needs it: a `Retrieval.candidates`
    field.
    """
    result = await retrieve(
        question,
        session,
        vector_store,
        embedder,
        top_k=RERANK_N,
        candidate_k=DEFAULT_CANDIDATE_K,
        query_vector=query_vector,
        arm=arm,
        reranker=reranker,
        rerank_n=RERANK_N,
    )
    return [h.chunk_id for h in result.hits]


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _collapsed_abstention(report: EvaluationReport) -> dict[str, Any]:
    """One row across all three arms, or a refusal to collapse.

    Collapsing is only honest when every arm scored the SAME near-miss items. If
    they did not, this says so and reports the per-arm counts as the reason —
    it never averages three rates fitted on three different populations.
    """
    scored = {arm: frozenset(report.near_miss[arm].scored_ids) for arm in ARMS}
    identical = len(set(scored.values())) == 1
    shared = sorted(next(iter(scored.values()))) if identical else []
    return {
        "n_near_miss": report.n_unanswerable,
        "abstention_top_k": report.abstention_top_k,
        "reranker": report.reranker,
        "arms_scored_identical_sets": identical,
        "coverage": f"{len(shared)}/{report.n_unanswerable}" if identical else None,
        "scored_ids": shared,
        "per_arm_scored_counts": {arm: report.near_miss[arm].n_scored for arm in ARMS},
        "rate": None,
        "rate_reason": (
            "no abstention decision is wired: `rrf_score` is a rank-position function and "
            "cannot signal abstention (DECISIONS.md 2026-08-19), and the swap to the "
            "reranker's score is re-homed to chunk 7's Guardrail threshold "
            "(DECISIONS.md 2026-08-23). Coverage is measured; a rate is not."
        ),
        "unscored_ids": sorted(set(report.near_miss[ARMS[0]].retrieved) - set(shared))
        if identical
        else [],
        "unscored_reason": UNSCORED_REASON,
        "why_not_per_arm": (
            "per-arm abstention is not reportable at ABSTENTION_TOP_K = "
            f"{report.abstention_top_k}. "
            "The decision reads hits[0] only. Without the reranker the arms score near-disjoint "
            "near-miss sets (lexical 2/6, vector 2/6, hybrid 4/6; lexical n vector = 1 item), so "
            "their rates are fitted on different question populations. With the reranker all "
            "three return an identical top-1 on every near-miss, so their rates are identical by "
            "construction rather than by merit. Neither configuration discriminates between arms."
        ),
    }


async def _u05_trajectory(
    session: AsyncSession,
    vector_store: VectorStore,
    embedder: Embedder,
    reranker: Reranker,
    question: str,
    query_vector: list[float],
    lure: set[str],
    pre_pools: dict[str, list[str]],
) -> dict[str, Any]:
    """u05's LURE chunk, pre-rerank rank per arm and post-rerank rank per arm.

    u05 is a near-miss unanswerable, so the chunk tracked here is the passage the
    system is meant to be tempted to answer from -- NOT a gold chunk, and the
    reranker lifting it to rank 1 is not a retrieval success. That is what makes
    this the evidence it is: the cross-encoder converges all three arms onto one
    top-1 regardless of whether that top-1 is correct.

    The pre-rerank ranks come from the pools already computed for the overlap
    evidence, so the same list backs both artifacts and they cannot disagree.
    """
    observed_pre: dict[str, int | None] = {}
    observed_post: dict[str, int | None] = {}
    for arm in ARMS:
        observed_pre[arm] = next(
            (i + 1 for i, cid in enumerate(pre_pools[arm]) if cid in lure), None
        )
        result = await retrieve(
            question,
            session,
            vector_store,
            embedder,
            top_k=RERANK_N,
            candidate_k=DEFAULT_CANDIDATE_K,
            query_vector=query_vector,
            arm=arm,
            reranker=reranker,
            rerank_n=RERANK_N,
        )
        observed_post[arm] = next((h.rerank_rank for h in result.hits if h.chunk_id in lure), None)

    return {
        "lure_chunk_ids": sorted(lure),
        "pre_rerank_rank": {"expected": U05_EXPECTED_PRE_RERANK, "observed": observed_pre},
        "post_rerank_rank": {"expected": U05_EXPECTED_POST_RERANK, "observed": observed_post},
        "matches_expectation": (
            observed_pre == U05_EXPECTED_PRE_RERANK and observed_post == U05_EXPECTED_POST_RERANK
        ),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(prog="chunk5_benchmark", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    settings = get_settings()
    embedder = _build_embedder(settings)
    engine = create_engine(settings)
    vector_store = ChromaVectorStore(settings.chroma_persist_dir)
    session_factory = create_session_factory(engine)

    reranker: Reranker | None = None
    try:
        async with session_factory() as session:
            # --- 1. the gate, no reranker ------------------------------------
            print("pass 1/2 — regression gate (--reranker none)...", file=sys.stderr)
            gate_report = await evaluate(session, vector_store, embedder, reranker=NOOP_RERANKER)
            gate_figures = _tuples(gate_report)
            try:
                assert_gate(gate_report)
            except GateMismatch as exc:
                print(f"\nGATE FAILED — the reranked pass was not run.\n{exc}", file=sys.stderr)
                return 1
            print("gate PASS — all 12 no-rerank figures reproduce exactly.\n")

            # Built only now: a failed gate must not even load the model.
            reranker = _build_reranker(settings, "local")

            # --- 2. answerable, reranker ON ----------------------------------
            print("pass 2/2 — answerable metrics (--reranker local)...", file=sys.stderr)
            report = await evaluate(session, vector_store, embedder, reranker=reranker)
            reranked = _tuples(report)
            delta = [
                f"{float(h) - float(v):+.3f}"
                for h, v in zip(reranked["hybrid"], reranked["vector"], strict=True)
            ]

            # --- 3/4. evidence ------------------------------------------------
            print("evidence — pre-rerank candidate pools...", file=sys.stderr)
            entries = load_golden_set(GOLDEN_SET_PATH)
            near_misses = [e for e in entries if not e.answerable]
            gold_by_id = await resolve_gold_chunk_ids(session, entries)
            vectors = await load_query_vectors({e.id: e.question for e in entries}, embedder)

            overlap: dict[str, Any] = {}
            pools_by_id: dict[str, dict[str, list[str]]] = {}
            for entry in near_misses:
                pools = {
                    arm: await _pool(
                        entry.question, vectors[entry.id], arm, session, vector_store, embedder
                    )
                    for arm in ARMS
                }
                pools_by_id[entry.id] = pools
                overlap[entry.id] = {
                    "pool_sizes": {arm: len(pools[arm]) for arm in ARMS},
                    "jaccard": {
                        f"{a}|{b}": round(_jaccard(pools[a], pools[b]), 4)
                        for a, b in itertools.combinations(ARMS, 2)
                    },
                }

            print("evidence — u05 lure trajectory...", file=sys.stderr)
            u05 = next(e for e in near_misses if e.id == "u05")
            trajectory = await _u05_trajectory(
                session,
                vector_store,
                embedder,
                reranker,
                u05.question,
                vectors[u05.id],
                gold_by_id[u05.id],  # u05's near_miss_to chunk: its LURE
                pools_by_id[u05.id],
            )

            results = {
                "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "step": "03",
                "chunk": 5,
                "manifest": await _manifest(
                    session, vector_store, embedder, settings, report, u05.document_id
                ),
                "config": {
                    "golden_set_path": report.golden_set_path,
                    "golden_set_version": report.golden_set_version,
                    "n_answerable": report.n_answerable,
                    "n_unanswerable": report.n_unanswerable,
                    "rrf_k": report.rrf_k,
                    "candidate_k": report.candidate_k,
                    "rerank_n": report.rerank_n,
                    "mrr_depth": report.mrr_depth,
                    "abstention_top_k": report.abstention_top_k,
                },
                "metrics": list(METRIC_LABELS),
                "gate": {
                    "reranker": gate_report.reranker,
                    "status": "PASS",
                    "expected": {arm: list(GATE_FIGURES[arm]) for arm in ARMS},
                    "observed": gate_figures,
                },
                "answerable_reranked": {
                    "reranker": report.reranker,
                    "arms": reranked,
                    "hybrid_minus_vector": delta,
                },
                "abstention_collapsed": _collapsed_abstention(report),
                "evidence": {
                    "pre_rerank_pool_overlap": overlap,
                    "u05_lure_trajectory": trajectory,
                },
            }
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(results, indent=2) + "\n")
    finally:
        if reranker is not None:
            reranker.close()
        vector_store.close()
        await engine.dispose()

    _print_summary(results, args.out)
    if not trajectory["matches_expectation"]:
        print(
            "\nSTOP: u05's observed ranks differ from the recorded expectation. Both are in "
            f"{args.out}; the expectation has NOT been adjusted. Retrieval order changed — "
            "explain that before reading any figure above.",
            file=sys.stderr,
        )
        return 1
    return 0


def _print_summary(results: dict[str, Any], out: Path) -> None:
    cfg = results["config"]
    print(
        f"golden set: {cfg['golden_set_path']} (v{cfg['golden_set_version']}) — "
        f"{cfg['n_answerable']} answerable, {cfg['n_unanswerable']} near-miss"
    )
    print(
        f"rrf_k={cfg['rrf_k']} candidate_k={cfg['candidate_k']} rerank_n={cfg['rerank_n']} "
        f"mrr_depth={cfg['mrr_depth']} abstention_top_k={cfg['abstention_top_k']}\n"
    )

    header = f"{'arm':<10}" + "".join(f"{label:>11}" for label in METRIC_LABELS)
    print(
        f"ANSWERABLE, reranker={results['answerable_reranked']['reranker']} "
        f"(n={cfg['n_answerable']}):"
    )
    print(header)
    for arm in ARMS:
        row = results["answerable_reranked"]["arms"][arm]
        print(f"{arm:<10}" + "".join(f"{v:>11}" for v in row))
    print(
        f"{'h - v':<10}"
        + "".join(f"{v:>11}" for v in results["answerable_reranked"]["hybrid_minus_vector"])
    )

    ab = results["abstention_collapsed"]
    print(f"\nABSTENTION — ONE collapsed row across all {len(ARMS)} arms:")
    if ab["arms_scored_identical_sets"]:
        print(
            f"  coverage {ab['coverage']}  rate: n/a  "
            f"scored-set {','.join(ab['scored_ids']) or '(none)'} (identical in every arm)"
        )
    else:
        print(
            "  NOT COLLAPSIBLE — the arms scored different items: "
            + ", ".join(
                f"{arm} {n}/{ab['n_near_miss']}" for arm, n in ab["per_arm_scored_counts"].items()
            )
        )
    print(f"  rate: {ab['rate_reason']}")
    if ab["unscored_ids"]:
        print(f"  unscored: {','.join(ab['unscored_ids'])} — {ab['unscored_reason']}")
    print(f"  footnote: {ab['why_not_per_arm']}")

    print("\nEVIDENCE — pre-rerank candidate-pool Jaccard, per near-miss:")
    pairs = [f"{a}|{b}" for a, b in itertools.combinations(ARMS, 2)]
    print(f"  {'id':<6}" + "".join(f"{p:>22}" for p in pairs))
    for qid, row in results["evidence"]["pre_rerank_pool_overlap"].items():
        print(f"  {qid:<6}" + "".join(f"{row['jaccard'][p]:>22.4f}" for p in pairs))

    t = results["evidence"]["u05_lure_trajectory"]
    print("\nEVIDENCE — u05 LURE chunk (near-miss, not a gold chunk), pre-rerank -> post-rerank:")
    for arm in ARMS:
        print(
            f"  {arm:<10} pre {str(t['pre_rerank_rank']['observed'][arm]):>4} "
            f"(expected {t['pre_rerank_rank']['expected'][arm]:>2})    "
            f"post {str(t['post_rerank_rank']['observed'][arm]):>4} "
            f"(expected {t['post_rerank_rank']['expected'][arm]:>2})"
        )
    print(f"\nresults written to {out}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
