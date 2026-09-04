"""The far-field gate's cut, at the boundary and at every edge.

    uv run --no-sync python -m tests.test_far_field_gate

Literal distances only. This test loads NO fixture and opens NO connection:
`tests/fixtures/query_embeddings.json` is untracked and rewritten on any cache
miss, so a test that read it would pass or fail depending on a file the repo
does not contain. Every number below is quoted from commit `bc1e261` and lives
in this file, which is committed.

$0 and offline by construction — no embedder, no Postgres, no vector store.
"""

from __future__ import annotations

from app.services.retrieval import (
    FAR_FIELD_ABSTAIN_DISTANCE,
    FarFieldInputError,
    Retrieval,
    RetrievedChunk,
    far_field_gate,
)

# The three class-1 out-of-domain probes recorded in commit `bc1e261` as falling
# BELOW the cut: the known 25% miss on the easiest possible positive class. They
# are asserted to come back ANSWER_UNVERIFIED because that is what the adopted
# rule does, not because it is the desired answer. If a future calibration moves
# the constant, these are the assertions that must be re-argued rather than
# quietly updated.
KNOWN_MISSES = {"ood08": 1.4131, "ood01": 1.4898, "ood10": 1.4906}

# a01 set the constant: it is the maximum observed answerable top-1 distance.
# `>=` is inclusive, so the question the rule was read off is itself refused.
BOUNDARY_ANSWERABLE = {"a01": 1.4932}


def _retrieval(distance: float | None, *, hits: int = 1) -> Retrieval:
    """A Retrieval carrying `hits` chunks whose top-1 has `distance`.

    Every other field is filler: the gate reads `hits[0].vector_distance`
    and nothing else, and the test says so by making the rest obviously inert.
    """
    chunk = RetrievedChunk(
        chunk_id="chk_test",
        document_id="doc_test",
        document_name="test",
        page=1,
        char_start=0,
        char_end=1,
        text="",
        rrf_score=0.0,
        lexical_rank=None,
        lexical_score=None,
        vector_rank=1,
        vector_distance=distance,
    )
    return Retrieval(hits=[chunk] * hits)


def main() -> None:
    assert FAR_FIELD_ABSTAIN_DISTANCE == 1.4932, FAR_FIELD_ABSTAIN_DISTANCE

    print("class-1 OOD below the cut — the recorded 25% known miss (bc1e261):")
    for qid, distance in KNOWN_MISSES.items():
        decision = far_field_gate(_retrieval(distance))
        assert decision.verdict == "ANSWER_UNVERIFIED", f"{qid}: {decision}"
        assert decision.top1_distance == distance, decision
        print(f"  {qid} {distance} -> {decision.verdict}  (NOT refused, as recorded)")

    print("\nboundary — `>=` is inclusive:")
    for qid, distance in BOUNDARY_ANSWERABLE.items():
        decision = far_field_gate(_retrieval(distance))
        assert decision.verdict == "ABSTAIN_OUT_OF_DOMAIN", f"{qid}: {decision}"
        print(f"  {qid} {distance} == the constant -> {decision.verdict}")

    # One nextafter below the constant must flip, or `>=` is not what is running.
    just_under = 1.4931999
    assert far_field_gate(_retrieval(just_under)).verdict == "ANSWER_UNVERIFIED"
    print(f"  {just_under} (just under) -> ANSWER_UNVERIFIED")

    print("\nclearly far out-of-domain:")
    far = 1.7829  # the largest class-1 OOD top-1 distance recorded in bc1e261
    decision = far_field_gate(_retrieval(far))
    assert decision.verdict == "ABSTAIN_OUT_OF_DOMAIN", decision
    print(f"  {far} -> {decision.verdict}")

    print("\nedge cases — each raises rather than inventing a verdict:")
    for label, retrieval in (
        ("empty retrieval (no hits)", Retrieval(hits=[])),
        ("top hit carries no vector_distance", _retrieval(None)),
        ("non-finite top-1: NaN", _retrieval(float("nan"))),
        ("non-finite top-1: +inf", _retrieval(float("inf"))),
        ("non-finite top-1: -inf", _retrieval(float("-inf"))),
    ):
        try:
            decision = far_field_gate(retrieval)
        except FarFieldInputError as exc:
            print(f"  {label}: FarFieldInputError({str(exc)[:48]}...)")
        else:
            raise AssertionError(f"{label} returned {decision} instead of raising")

    # NaN is the one that would pass silently under a bare `>=`: every
    # comparison against it is False, so an unguarded rule answers on it.
    assert not (float("nan") >= FAR_FIELD_ABSTAIN_DISTANCE)

    print("\nANSWER is unreachable from this path, by construction:")
    verdicts = {far_field_gate(_retrieval(d)).verdict for d in (0.0, 1.0, 1.4931, 1.4932, 9.9)}
    assert "ANSWER" not in verdicts, verdicts
    print(f"  verdicts reachable from far_field_gate(): {sorted(verdicts)}")

    print("\nok: boundary inclusive, 3 known misses reproduce, 5 edge cases raise")


if __name__ == "__main__":
    main()
