# Step 03 · Chunk 3 — re-baseline on golden set v3

**Run date:** 2026-08-20 · **No reranker** (that is chunk 4) · `rrf_k=5`, `candidate_k=30`,
`voyage-4-lite` @ 1024 dims · corpus `doc_01M0D39WZDYY7STA3PHWT5R4C7`, 260 chunks, 260 vectors.

Both fixtures were scored by the same code path (`load_golden_set` normalises v2 and v3), so a
change in the numbers is the benchmark, not the harness.

---

## 0. Regression check — does v2 still reproduce?

Three retrieval-path files changed since the Step 02 run, all in the CLI chunks: `retrieval.py`
(added `rrf_k` and `arm` parameters, both defaulting to the prior behaviour), `embeddings.py` (two
log lines on retry paths), `vector_store.py` (new read-only `count_by_document`). Inspection said
all three are inert at this configuration. The v2 arm tests that claim empirically.

| arm | recall@5 | recall@10 | MRR | recorded 2026-08-19 | verdict |
| --- | --- | --- | --- | --- | --- |
| lexical-only | 0.409 | 0.727 | 0.479 | 0.409 / 0.727 / 0.479 | reproduces |
| vector-only | 0.727 | 0.818 | 0.690 | 0.727 / 0.818 / 0.690 | reproduces |
| hybrid (RRF k=5) | 0.818 | 0.818 | 0.633 | 0.818 / 0.818 / 0.633 | reproduces |

Abstention also reproduces: q09 `0.24359`, q10 `0.29167` against the recorded 0.244 / 0.292.

(The MRR column here is the same unbounded pre-`MRR_DEPTH` key flagged in §1 below.)

**Exact reproduction on all eleven figures.** Two things follow. The three changed files are
behaviourally inert at this configuration, as inspection claimed. And the new offset-based gold
resolution produces gold sets identical to v2's stored `chunk_ids` — already proven directly in
`evaluation.py`'s self-check, now confirmed end to end through the metrics.

This run cost $0: no `--allow-network`, so a cache miss would have raised rather than spent.

---

## 1. v3 results — answerable only, n=30

| arm | recall@5 | recall@10 | MRR |
| --- | --- | --- | --- |
| lexical-only | 0.233 | 0.400 | 0.152 |
| vector-only | **0.833** | **0.900** | **0.707** |
| hybrid (RRF k=5) | 0.667 | 0.867 | 0.450 |

> **⚠ The MRR column above is a DIFFERENT KEY from `mrr@10`. Do not build a gate from it.**
>
> These three figures (0.707 / 0.450 / 0.152) are the **unbounded** MRR this harness computed
> before `MRR_DEPTH` existed — it silently equalled whatever `top_k` the caller passed, which
> here was 30. Chunk 4 replaced it with MRR cut at a fixed depth of 10
> (`app/services/evaluation.py`, `MRR_DEPTH`); that was a **definition change, not drift**, and
> these numbers do not reproduce at depth 10 by construction.
>
> The live key is **`mrr@10`**, and its recorded no-rerank values are **0.700 (vector) / 0.445
> (hybrid) / 0.142 (lexical)** — frozen in `evaluation.GATE_FIGURES` and asserted for exact
> equality by `scripts/chunk5_benchmark.py`. **A gate written from the column above fails on all
> three arms**, and would be read as corpus drift when nothing had drifted.
>
> The recall columns are unaffected — `RECALL_KS` did not change. Only MRR was re-based.
> This note exists so nobody writes a gate from this column again.

Same run, same questions — hybrid minus vector-only:

| metric | difference |
| --- | --- |
| recall@5 | **-0.167** |
| recall@10 | -0.033 |
| MRR | **-0.257** |

At n=30, one question is 3.3 recall points (against 9.1 at n=11).

---

## 2. v2 -> v3 delta, both arms

| arm | metric | v2 (n=11) | v3 (n=30) | delta |
| --- | --- | --- | --- | --- |
| lexical | recall@5 | 0.409 | 0.233 | **-0.176** |
| lexical | recall@10 | 0.727 | 0.400 | **-0.327** |
| lexical | MRR | 0.479 | 0.152 | **-0.327** |
| vector | recall@5 | 0.727 | 0.833 | +0.106 |
| vector | recall@10 | 0.818 | 0.900 | +0.082 |
| vector | MRR | 0.690 | 0.707 | +0.017 |
| hybrid | recall@5 | 0.818 | 0.667 | **-0.152** |
| hybrid | recall@10 | 0.818 | 0.867 | +0.048 |
| hybrid | MRR | 0.633 | 0.450 | **-0.182** |

The two sets are different question populations, not a before/after on the same questions: v2 has
11 answerable entries, v3 has 30, and none are shared. The delta shows how much the arms' measured
standing depends on which benchmark they are measured against, not a change in retrieval.

---

## 3. Abstention — 6 near-misses, scored separately

Top-1 hybrid `rrf_score`; never folded into recall or MRR.

| id | top-1 rrf_score |
| --- | --- |
| u03 | 0.33333 |
| u01 | 0.30952 |
| u02 | 0.26786 |
| u04 | 0.25000 |
| u05 | 0.24286 |
| u06 | 0.21212 |

Range 0.212–0.333. The Step 02 finding that `rrf_score` does not separate answerable from
unanswerable queries is unchanged in shape here — these six sit inside the same band the v2
unanswerables occupied (0.244, 0.292).

**Not yet scored:** per the chunk 5 requirement recorded in `DECISIONS.md` (2026-08-20), an
abstention only counts if the `near_miss_to` span was actually in the retrieved set. That check is
chunk 5's, is not implemented, and no abstention figure above should be read as a pass rate.

---

## 4. Observation, flagged as hypothesis not finding

The lexical arm falls hardest of the three (recall@10 -0.327, MRR -0.327). v3's questions were
deliberately authored to avoid the passage's vocabulary, with overlap measured by the same stemmer
the lexical arm uses; v2's were not. A lexical arm scoring worse on questions written not to share
words with their evidence is the expected direction, but this run does not isolate that cause from
the change in n, the change in question population, or the change in evidence spans.

**The DoD #5 verdict is chunk 5's**, after the reranker exists. Nothing here should be read as
settling it.

---

## 5. Cost

| item | value |
| --- | --- |
| v2 arm | $0 — 13/13 cached from Step 02, `allow_network=False` |
| v3 arm | 36 questions, 3,366 chars, ~841 tokens (char/4), **one** Voyage request |
| actual spend | **~$0.0000168** at $0.02/1M, inside the 200M free tier |
| cache | 13 -> 49 entries; every rerun of either fixture is now $0 |
| corpus | untouched — no re-embed, 260 vectors before and after |
