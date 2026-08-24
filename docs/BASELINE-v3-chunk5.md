# Step 03 · Chunk 5 — DoD #5 verdict on golden set v3

**Run date:** 2026-08-22 · `rrf_k=5`, `candidate_k=30`, `rerank_n=40`, `mrr_depth=10`,
`abstention_top_k=1` · `voyage-4-lite` @ 1024 dims · corpus
`doc_01M0D39WZDYY7STA3PHWT5R4C7`, 260 chunks, 260 vectors · reranker: local pinned int8
ONNX `cross-encoder/ms-marco-MiniLM-L6-v2`.

Every figure below is computed and persisted in **`docs/chunk5-results.json`**, written by
`scripts/chunk5_benchmark.py` in the same run that produced this document. Nothing here is
asserted from memory; the JSON is the source and this file is the reading of it.

Reproduce with:

```bash
uv run python -m scripts.chunk5_benchmark
```

---

## 0. Regression gate — the corpus and index did not move

The reranked pass does not run until the twelve no-rerank figures reproduce **exactly**, as
`format_report` prints them, against `evaluation.GATE_FIGURES`. This is not a tolerance and
not a threshold: the benchmark corpus is do-not-re-embed and unrebuildable, so a moved
figure means the one irreplaceable input changed.

| arm | recall@5 | recall@10 | recall@30 | mrr@10 |
| --- | --- | --- | --- | --- |
| lexical | 0.233 | 0.400 | 0.567 | 0.142 |
| vector | 0.833 | 0.900 | 1.000 | 0.700 |
| hybrid | 0.667 | 0.867 | 0.967 | 0.445 |

**Result: PASS**, 12/12 exact. Expected and observed are both recorded in the JSON, so the
gate's own inputs are auditable rather than implicit.

> Note on which MRR this is. The fourth column is **mrr@10**. `docs/BASELINE-v3-chunk3.md`
> §1 records MRR as 0.707 / 0.450 / 0.152 — the *unbounded* metric from before `MRR_DEPTH`
> existed. Those are a different key, not drift, and are not what the gate compares.

The gate was also negatively verified: perturbing one figure by a single thousandth aborts
the run at pass 1, names the arm and both tuples, writes no results file, and **never
constructs the reranker**.

---

## 1. Answerable metrics, reranker ON — all three arms

n = 30 answerable entries. Near-misses never enter these numbers.

| arm | recall@5 | recall@10 | recall@30 | mrr@10 |
| --- | --- | --- | --- | --- |
| lexical | 0.500 | 0.533 | 0.567 | 0.489 |
| vector | **0.733** | **0.867** | **1.000** | **0.624** |
| hybrid | 0.733 | 0.867 | 0.967 | 0.621 |

Hybrid minus vector-only, same run, same questions:

| metric | hybrid − vector |
| --- | --- |
| recall@5 | +0.000 |
| recall@10 | +0.000 |
| recall@30 | **−0.033** |
| mrr@10 | **−0.003** |

### Does hybrid beat vector-only? No.

**Hybrid does not beat vector-only on any of the four metrics.** It ties on two and loses
on two.

- **mrr@10: hybrid 0.621 against vector 0.624, −0.003.** Below the resolution of this
  benchmark — one question is worth 3.3 recall points here — so this is a **tie**, not a
  hybrid loss. It must not be quoted as "hybrid nearly won".
- **recall@30: hybrid 0.967 against vector 1.000, −0.033.** The one number that separates
  the arms — and it is not a reranked result. See below.

### The falsifying number is a pool-membership fact, not a reranking result

**recall@30 is identical to the pre-rerank gate tuple in every arm** — vector 1.000 and
1.000, hybrid 0.967 and 0.967, lexical 0.567 and 0.567. Reranking did not move it, and for
the single arms it *could* not: `candidate_k = 30`, so vector's candidate pool is exactly
30 chunks and reordering within it cannot change which chunks are in the top 30. Hybrid's
pool is 40, so its recall@30 was free to move — and did not.

So the one number that separates hybrid from vector-only **was already true before the
cross-encoder ran**. It is a statement about pool membership: RRF fuses lexical's 30 and
vector's 30 and cuts to 40, and in doing so it **drops a gold chunk out of the candidate
pool entirely** — one that vector-only had. No amount of reranking can recover a chunk
that is not in the pool handed to it.

**The consequence, stated directly: fusion's only measurable net effect on answerable
retrieval is that it loses one gold chunk from the candidate pool.** Everything fusion
contributed at depth ≤ 10 was fully absorbed by the cross-encoder — hybrid and vector are
**identical at recall@5 (0.733) and recall@10 (0.867)**, and 0.003 apart at mrr@10. Fusion
adds nothing the reranker does not already extract from the vector pool alone, and it
subtracts one chunk. That is the whole of its measured contribution.

Reranking did not rescue fusion; it **converged** the two arms. Before the reranker, hybrid
trailed vector by −0.166 recall@5 and −0.255 mrr@10; after it, the two are identical at
both recall depths. The cross-encoder repairs the damage RRF did at shallow depth — it
does not convert fusion into an advantage, and it cannot undo the pool loss at depth 30.

Reranking also made vector-only **worse** on the shallow metrics (recall@5 0.833 → 0.733,
mrr@10 0.700 → 0.624) while lifting lexical hard (mrr@10 0.142 → 0.489). Recorded flat;
this contradicts the stated prediction that the reranker would recover precision.

---

## 2. Abstention — ONE collapsed row, not three

| scope | coverage | rate | scored-set |
| --- | --- | --- | --- |
| all three arms | 5/6 | n/a | u01, u02, u03, u04, u05 — identical in every arm |

Collapsing is legitimate here **only because the three arms scored the same five items**;
the driver checks that and refuses to collapse if they ever diverge.

**Why there is no rate.** No abstention decision is wired. `rrf_score` is a function of
rank position and cannot signal abstention (DECISIONS.md 2026-08-19), and the swap to the
reranker's relevance score is re-homed to chunk 7's Guardrail threshold (DECISIONS.md
2026-08-23). Coverage is measured; a rate is not.

**Why this is not reported per arm — the footnote that belongs with any quotation of it.**
Per-arm abstention is not reportable at `ABSTENTION_TOP_K = 1`. The decision reads
`hits[0]` and nothing else, and in both configurations that single hit destroys the
comparison:

- **Without the reranker**, the arms score near-disjoint near-miss sets — lexical 2/6,
  vector 2/6, hybrid 4/6, with lexical ∩ vector = exactly one item. Three rates fitted on
  three different question populations are not comparable, whatever the signal.
- **With the reranker**, all three arms return an identical top-1 on every near-miss. Their
  rates would then be identical **by construction**, not by merit.

**Why coverage is 5/6 and not 6/6 — u06's lure never fired.** Diagnosed by direct
inspection, because an unexplained missing item is the first thing a reviewer asks about.
**No filter, threshold or fixture condition excluded it.**

u06 is a near-miss unanswerable, so its `near_miss_to` chunk is the **lure** — the passage
the system is supposed to be tempted to answer from — not a correct answer. Nothing was
demoted in the sense that matters; the designated lure simply never reached rank 1.

u06 asks for the per-document price of the technique that prepends a generated summary
before embedding. The fixture's lure is `chk_01M0D4BMGPC4TSS179JBQE32HB` (p95 — the
*agentic*-chunking pricing passage). All three arms instead rank
`chk_01M0D4BMGPTW90AQ81EM4R32VW` (p90 — the passage describing the *technique* the question
names, "prepend this context to the chunk before embedding") at rank 1. The lure's observed
ranks: **not in pool at all (lexical)**, **2 → 6 (vector)**, **8 → 6 (hybrid)**.

**The consequence, stated as what it is:** at `ABSTENTION_TOP_K = 1` the decision reads only
`hits[0]`, so the lure was never placed in front of it in any arm. **u06's abstention
behaviour was therefore never exercised — it is untested, not passed.** Counting it as
either a correct abstention or a failure would be inventing a result the run did not
produce, which is exactly what the UNSCORED outcome exists to prevent.

This is **not** an instance of the cross-encoder demoting a correct chunk (the vector
recall@5 regression carried into chunk 7). That is a correct-answer demotion on the
**answerable** set; this is a lure that failed to fire on the **unanswerable** set. They are
different phenomena and must not be pooled — chunk 7 calibrates an abstention threshold, and
feeding it a lure-miss as if it were a relevance error would corrupt that calibration.

Separately, the fact that retrieval consistently prefers the p90 chunk over the p95 chunk
is a recorded **limitation of golden set v3's near-miss set**, not a retrieval fault — see
DECISIONS.md 2026-08-23.

---

## 3. Evidence — why arm choice stops mattering at rank 1

Both artifacts below are computed values in `docs/chunk5-results.json`, not claims.

### 3.1 Pre-rerank candidate pools barely overlap

Pairwise Jaccard of the candidate pools each arm hands the reranker, per near-miss query.
Pools are read through `retrieve()` itself with the NoOp reranker at full pool depth, so
they are the same lists the real path builds.

| id | pool sizes (lex/vec/hyb) | lexical ∣ vector | lexical ∣ hybrid | vector ∣ hybrid |
| --- | --- | --- | --- | --- |
| u01 | 30 / 30 / 40 | 0.3043 | 0.5909 | 0.6667 |
| u02 | 30 / 30 / 40 | 0.1765 | 0.4894 | 0.5909 |
| u03 | 30 / 30 / 40 | 0.2000 | 0.5556 | 0.5556 |
| u04 | 30 / 30 / 40 | 0.1111 | 0.4894 | 0.4894 |
| u05 | 28 / 30 / 40 | **0.0741** | 0.4783 | 0.4583 |
| u06 | 30 / 30 / 40 | 0.1765 | 0.5556 | 0.5217 |

Lexical and vector never share more than a third of their pools, and on **u05 they share
0.0741 — four chunks in common across a 54-chunk union**. (u05 is also the one query where
lexical returns fewer than `candidate_k`: 28, not 30.) Those two arms are looking at almost
entirely different evidence. They still return the same top-1.

### 3.2 u05's lure: three different starting ranks, one destination

u05 is a near-miss unanswerable — *"Which research group is behind BEIR?"* — so the chunk
tracked below is its **lure**, the passage at `[224441, 224704)`, p199 that the corpus never
actually answers the question from. It is **not** a gold chunk, and the reranker lifting it
is not a retrieval success.

| arm | pre-rerank rank of the lure | post-rerank rank |
| --- | --- | --- |
| lexical | 12 | **1** |
| vector | 17 | **1** |
| hybrid | 9 | **1** |

Observed matches the recorded expectation (12 / 17 / 9 → 1 / 1 / 1) exactly. The driver
would have exited non-zero and reported the observed values rather than adjust the
expectation.

**The mechanism, and it is stronger than a gold-chunk reading would be:** the arm selects
the candidate pool; the cross-encoder decides what surfaces from it — **regardless of
whether what surfaces is a correct answer.** Here it takes three pools overlapping as little
as Jaccard 0.0741, starting the same chunk at ranks 12, 17 and 9, and converges all three on
an identical rank-1 hit **that is a lure**. Convergence is a property of the cross-encoder's
ranking, not evidence that the ranking is right.

That is exactly why per-arm abstention comparison is structurally unavailable at
`ABSTENTION_TOP_K = 1`: the decision reads `hits[0]`, and the cross-encoder has already made
`hits[0]` the same chunk in every arm. The arms are the same system at the point the
decision reads, whatever that chunk turns out to be.

**u05 and u06 are the same phenomenon with opposite outcomes**, and the pair is the clearest
statement of the finding:

| | lure's post-rerank rank | outcome | what it means |
| --- | --- | --- | --- |
| **u05** | **1** in all three arms | **SCORED** | the lure was put in front of the decision — abstention behaviour is exercised |
| **u06** | 6 (vector, hybrid); never pooled by lexical | **UNSCORED** | the lure never reached the decision — abstention behaviour is untested, not passed |

In both cases the three arms agree on the top-1 and the arm choice is irrelevant. The only
difference is *which* chunk the cross-encoder converged them onto: u05's designated lure, or —
for u06 — a chunk the fixture never designated (see §2 and the v3 limitation logged in
DECISIONS.md 2026-08-23).

---

## 4. Cost

| item | value |
| --- | --- |
| embedding calls | **zero** — `allow_network` never passed; a cache miss would have raised |
| spend | **$0** |
| corpus | untouched, 260 chunks / 260 vectors before and after; golden set v3 unedited |
| wall clock | gate pass + reranked pass + evidence, one run, ~5 min on this i7-8665U |
