"""Cross-process determinism of the vector arm.

    uv run --no-sync python -m tests.test_vector_search_stability

Asserts that all 36 golden questions return a bitwise-identical
`(id, rank, distance)` list at k=40 from `WORKERS` **separate OS processes**.

Separate processes, not an in-process loop, and that is the whole point. The
defect this replaces was fixed at index-load time: hnswlib built its graph
during the first query of a process and the layer assignment varied between
processes, so a loop inside one process saw one graph and reported perfect
stability while a second process disagreed. Any in-process repetition is blind
to it by construction.

All 36 questions, not the 8 in `retrieval._BASELINE_QIDS`. The baseline set is
a01 a05 a09 a13 a17 a21 a25 u01 and the measured-unstable set is
a02 a03 a15 a34 u02 u05 u06 (chunk 7.1g §3) — **disjoint**. The digest gate
therefore could not have caught this on any run, which is why it did not. The
seven are named individually in this test's output so their coverage is visible
rather than merely implied.

$0 and offline: query vectors come from the warm cache (`allow_network` stays
False, so a miss raises rather than spends) and no Postgres connection is
opened — the vector arm is the only thing under test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

from app.services.embeddings import FakeEmbedder
from app.services.evaluation import GOLDEN_SET_PATH, load_golden_set, load_query_vectors
from app.services.vector_store import ExactVectorStore, VectorStore

WORKERS = 12
TOP_K = 40
COLLECTION = "chunks_voyage4lite_1024"

# Measured under HNSW across 20 processes at k=40 (chunk 7.1g §3): every
# question that ever returned more than one distinct signature. Named here so
# the assertion output shows each one covered. `u03` is deliberately absent —
# the earlier handoff claimed it "varies at rank 3" and that was measured false.
UNSTABLE_UNDER_HNSW = ("a02", "a03", "a15", "a34", "u02", "u05", "u06")


def _build_store() -> VectorStore:
    """The store under test. A seam: the HNSW negative control swaps this."""
    return ExactVectorStore()


async def _run_worker() -> dict[str, list[tuple[str, int, str]]]:
    """One process's answer for every question.

    Distances are compared as `float.hex()`, not `repr()` or a rounded string:
    this is the one place that must be bitwise, since the whole claim is that
    two processes compute the identical float.
    """
    entries = load_golden_set(GOLDEN_SET_PATH)
    embedder = FakeEmbedder(dimensions=1024, model="voyage-4-lite")
    vectors = await load_query_vectors({e.id: e.question for e in entries}, embedder)

    store = _build_store()
    signatures: dict[str, list[tuple[str, int, str]]] = {}
    for entry in entries:
        hits = await store.query(COLLECTION, vectors[entry.id], TOP_K)
        signatures[entry.id] = [
            (hit.id, rank, hit.distance.hex()) for rank, hit in enumerate(hits, start=1)
        ]
    store.close()
    return signatures


def _spawn(workers: int) -> list[dict[str, list[tuple[str, int, str]]]]:
    runs: list[dict[str, list[tuple[str, int, str]]]] = []
    for i in range(workers):
        proc = subprocess.run(
            [sys.executable, "-m", "tests.test_vector_search_stability", "--worker"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"worker {i} exited {proc.returncode}:\n{proc.stderr}")
        raw: dict[str, list[list[Any]]] = json.loads(proc.stdout)
        runs.append({qid: [(h[0], h[1], h[2]) for h in hits] for qid, hits in raw.items()})
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()

    if args.worker:
        print(json.dumps(asyncio.run(_run_worker())))
        return

    assert args.workers >= 10, f"cross-process stability needs >= 10 processes, got {args.workers}"
    runs = _spawn(args.workers)

    qids = sorted(runs[0])
    assert len(qids) == 36, f"expected all 36 golden questions, got {len(qids)}"
    for i, run in enumerate(runs):
        assert sorted(run) == qids, f"worker {i} answered a different question set"

    unstable: list[str] = []
    for qid in qids:
        distinct = {tuple(run[qid]) for run in runs}
        if len(distinct) != 1:
            unstable.append(qid)
        assert len(runs[0][qid]) == TOP_K, f"{qid}: expected {TOP_K} hits, got {len(runs[0][qid])}"

    # Printed before the assertion, so a failure shows which of the seven moved.
    print(f"{args.workers} separate OS processes x {len(qids)} questions at k={TOP_K}")
    print("\nthe seven questions measured unstable under HNSW (chunk 7.1g §3):")
    for qid in UNSTABLE_UNDER_HNSW:
        assert qid in qids, f"{qid} is not in the golden set — the unstable set is stale"
        distinct = {tuple(run[qid]) for run in runs}
        verdict = "STABLE" if len(distinct) == 1 else f"*** VARIES: {len(distinct)} signatures ***"
        print(f"  {qid}: {len(distinct)} distinct (id,rank,distance) signature(s) -> {verdict}")

    print(f"\nother {len(qids) - len(UNSTABLE_UNDER_HNSW)} questions: ", end="")
    others = [q for q in qids if q not in UNSTABLE_UNDER_HNSW]
    print(f"{sum(len({tuple(r[q]) for r in runs}) == 1 for q in others)}/{len(others)} stable")

    assert not unstable, (
        f"the vector arm is NOT cross-process deterministic. Questions returning more "
        f"than one distinct (id, rank, distance) signature across {args.workers} "
        f"processes: {unstable}"
    )
    print(
        f"\nok: {len(qids)}/{len(qids)} questions bitwise-identical across "
        f"{args.workers} separate OS processes, including all "
        f"{len(UNSTABLE_UNDER_HNSW)} HNSW-unstable questions"
    )


if __name__ == "__main__":
    main()
