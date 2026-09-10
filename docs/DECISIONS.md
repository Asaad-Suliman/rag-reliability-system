# RAG Reliability System — Decisions Log

This is a working decisions log for the RAG Reliability System, kept during
development. It records decisions and the reasoning behind them as they were
made — including decisions that were later voided, which are left in place
and labelled `VOID`/`VOIDED` rather than deleted or rewritten. Split verdicts,
open questions, and measurements that contradicted the stated prediction are
recorded the same way they were when they happened.

It is mirrored from a private working vault and may lag the code slightly.

Newest entries first.

---

## 2026-09-10 — Chunks 8.5 + 8.6: the budget validator was certifying a counter demoted on 2026-08-25, and its "equal-cost" fixtures were false under ANY counter. Re-pointed to `TiktokenCounter`, fixtures repaired to prose, all ten checks run for real, `plan_context`'s dormant default exercised.

> **Line-citation convention for this entry.** Every path below is repo-root-relative and every
> citation is pinned to a named revision: source and `docs/DECISIONS.md` citations to **`b98fe1e`**,
> `09_Memory/DECISIONS.md` citations to **`9478234`** — the revisions immediately before this entry
> was written. Prepending an entry shifts every line number beneath it, so an unpinned self-citation
> is stale the moment it is committed. Files are cited by full path throughout:
> `app/api/v1/query.py` and `app/schemas/query.py` share a basename, and a bare `query.py` would be
> ambiguous between them.

**Decision, in one line.** `app/services/context_budget.py`'s `_check_offline` now runs under
`TiktokenCounter` — the counter `plan_context` actually defaults to — its three big-hit fixtures
are one shared prose text instead of three different repeated characters, and
`tests/test_budget_counter_default.py` exercises the `counter=None` default branch that nothing in
the tree had ever taken.

---

### A. What was wrong: the validator certified a counter nothing ships with

`HeuristicCharCounter` was demoted on **2026-08-25** (chunk 7 Phase E, entry below):
`app/services/context_budget.py`'s `plan_context` defaults to `TiktokenCounter`. But `_demo()`
constructed a `HeuristicCharCounter` and handed it to `_check_offline`, so all ten behavioural
checks — impossible-budget, `usable_budget` arithmetic, empty-in/empty-out, score-source
uniformity, tie-break, determinism, whole-chunk inclusion, exclusion accounting, the
top-hit-always-included invariant, `ChunkExceedsBudget`, and the manifest round-trip — were
certified against a counter production does not use. Found by the chunk 8.4 read-only inspection;
fixed here.

The manifest assert inside `_check_offline` hard-coded the demoted counter's identity, so it moved
with the re-point. See §D.

### B. The finding that stopped chunk 8.5: "equal cost" was false under any counter

Re-pointing alone did **not** work. `_check_offline` failed immediately at the binding-budget
check with `ChunkExceedsBudget: chunk chk_3 ... costs 936 tokens ... but the whole usable budget
is 620`.

Cause, measured rather than inferred. The three big-hit fixtures were built from three DIFFERENT
repeated characters and commented as equal-cost:

```
_fake_hit("chk_1", "x" * 1_400, rerank_score=3.0),  # big_cost
_fake_hit("chk_2", "y" * 1_400, rerank_score=2.0),  # 2 * big_cost total, fits
_fake_hit("chk_3", "z" * 1_400, rerank_score=1.0),  # would be 3 * big_cost, does not
```

while `big_tokens` was measured from **`chk_1`'s text only**. Under `TiktokenCounter` those three
strings cost **210 / 420 / 840** tokens — a 4x spread. Under `HeuristicCharCounter` they cost 400
each, because that counter is `ceil((len - non_ascii) / CHARS_PER_TOKEN) + non_ascii` and reads
length and nothing else.

**The comments were therefore false under any counter that is not a pure length ratio** — the
heuristic did not make them true, it only hid that they were not. This is a strictly worse finding
than "the checks ran under the wrong counter": the checks' expectations were *arithmetically
shaped around* the demoted counter. `plan_context` itself was correct throughout; nothing in the
production path was broken at any point.

Chunk 8.5 stopped here rather than adjusting the check, and committed nothing.

### C. The repair: one shared prose text, not one shared character

All three big hits now use a single `big_text`, so equal cost is a property of the fixtures
themselves rather than an assumption about the counter. `big_text` is
`(_FIXTURE_PROSE * 20)[:1_400]`, where `_FIXTURE_PROSE` is a synthetic English sentence defined
beside `_fake_hit` in `app/services/context_budget.py`.

**Prose rather than a repeated character, deliberately.** A 1,400-character run of one character is
a BPE extreme — long merges, absurdly few tokens — and would put the check in a tokenization regime
the corpus never enters. Measured: the repaired `big_text` is 1,400 chars → **336** tiktoken tokens
(≈4.17 chars/token) versus **400** under the heuristic, a 1.19x margin that sits inside the corpus's
own regime (260 real chunks, median margin 1.3820x). The old `"x" * 1_400` was 210 tokens, ≈6.7
chars/token — nothing in the corpus tokenizes like that.

**Synthetic rather than lifted from the corpus,** so re-ingesting data cannot move a fixture.
Byte-stable: no randomness, no clock, no hash seed.

**Scope of the repair, and one place the chunk 8.6 brief was not followed.** The brief said to
apply the repair to *every* fixture built from a repeated character. Four groups were examined and
only one was repaired:

| fixture group | asserts a cost relationship? | action |
| --- | --- | --- |
| `rrf_hits`/`rr_hits` (`"a"`/`"b"` × 100) | No — reads `.score_source` only; both fit the full budget | unchanged |
| `tied` (`"a"`/`"b"`/`"c"` × 100) | No — the comment says "equal **scores**", i.e. `rerank_score=2.5`, set explicitly per hit | unchanged |
| `small_tokens`/`chk_4` (`"w"` × 10) | Yes, and it **holds** — measured from the identical literal the fixture uses | unchanged |
| the three big hits | Yes, and it was **false** | repaired |

The brief's own qualifying criterion — "any fixture whose comment asserts a cost relationship must
actually have it under `TiktokenCounter`" — is what was applied; three of the four groups do not
meet it. Rewriting `tied` in particular would have blurred a check about identity ordering, which
the no-weakening rule forbids. Recorded rather than done silently.

`HeuristicCharCounter` was **not** touched and remains available to explicit callers. `counter` in
`_demo()` stays `HeuristicCharCounter` on purpose: it feeds the historical never-under-count check
against the vendored WordPiece reference, which is a property *of* that counter and meaningless
under any other.

### D. The manifest counter field moved — VOID CONVENTION

The `budget_manifest` block asserted inside `_check_offline` previously pinned
`"counter": "heuristic-char-3.5-v1"`. It now pins
`"counter": "tiktoken-cl100k_base-v1+1.2x-cross-tokenizer"`.

**The prior literal is VOID as of this entry** — not wrong when written, but naming a counter that
check no longer runs. Every other field is unchanged: `context_window` 200,000,
`prompt_scaffold_reserve` 2,000, `query_reserve` 512, `answer_reserve` 4,000,
`per_chunk_overhead_tokens` 96, `usable_budget` 193,488. `usable_budget` is pure reserve arithmetic
and independent of which counter runs.

The literal is spelled out rather than read off `TiktokenCounter.name` so that a change to
`CROSS_TOKENIZER_SAFETY_FACTOR` — which that name embeds — fails the check and gets looked at,
instead of following the constant silently.

**No committed measurement artifact moved.** `docs/chunk7-scores.json`'s `manifest.budget.counter`
still reads `heuristic-char-3.5-v1`, because it comes from
`scripts/chunk5_benchmark.py`'s `_manifest`, which constructs its own `HeuristicCharCounter()`
explicitly and never received the `_check_offline` parameter. `docs/chunk5-results.json` has no
`budget` block at all. Whether `scripts/chunk5_benchmark.py` should also re-point is **NOT
ESTABLISHED** — it was out of scope here and no rationale for either choice was decided.

### E. The four previously-unreached checks: first real results

Chunk 8.5's failure aborted `_check_offline` partway, leaving four checks with no result under
`TiktokenCounter` — recorded then as UNKNOWN, explicitly not as passing. They have now run:

| check | first result under `TiktokenCounter` |
| --- | --- |
| a smaller later chunk still fits after a larger one was skipped | **PASS** |
| the top-ranked hit is ALWAYS included | **PASS** |
| a single hit that cannot fit AT ALL raises `ChunkExceedsBudget` | **PASS** |
| (10) the manifest block round-trips and carries all five values + identity | **PASS** |

**Execution was verified, not inferred from a green exit.** A `sys.settrace` line-tracer over
`_check_offline(TiktokenCounter())` confirmed every one of the twelve comment-delimited blocks
executed at least one line — 108 distinct lines in total. A passing process exit proves only that
nothing raised; the trace proves each check ran.

### F. `plan_context`'s `counter=None` default had no exerciser at all

`app/services/context_budget.py`'s `if counter is None: counter = TiktokenCounter()` was taken by
**nothing in the tree**. Every call site passed a counter explicitly: the eleven inside
`_check_offline`, and `scripts/chunk7_scores.py`, whose own docstring says so — "the counter is
passed explicitly rather than relying on the default, so a measurement run does not become the
first real exercise of the dormant TiktokenCounter default path." That comment names the gap and
declines to fill it. The gap was therefore known and deliberate on the measurement side; what was
missing was a test.

`tests/test_budget_counter_default.py` (new; plain asserts, runnable as
`python -m tests.test_budget_counter_default`, no pytest) fills it with two properties:

1. **the branch is taken** — `plan_context(hits, budget)` with no counter yields
   `counter_name == TiktokenCounter.name`, and an explicitly passed counter still wins;
2. **a cold cache raises `TiktokenCacheMissing` from that branch** — a deployment defect surfacing
   in a test rather than on a live query.

**(2) runs in a subprocess, and had to.** `tiktoken.get_encoding` memoises into a module-global
`ENCODINGS` dict, so once any `TiktokenCounter()` has succeeded in a process, a later one returns
the memoised encoding, attempts no network call, and **cannot raise**. An in-process cold-cache
check written after any successful load would have asserted nothing and still looked green — the
same class of blindness `tests/test_vector_search_stability.py` avoids with separate OS processes.
The child gets a cold cache by running with its working directory set to an empty temporary
directory: `TIKTOKEN_CACHE_DIR` is `Path("models/tiktoken")`, a **relative** path, so a different
working directory is a missing cache — which is exactly the production failure it stands in for.

**Negative control, run to prove the check is not vacuous:** the identical child executed with its
working directory at the repo root (warm cache) prints
`NO RAISE -- the default branch loaded an encoding from a cold cache` and exits 1. Cold → exit 0,
warm → exit 1. The check discriminates.

### G. Verification

`python -m app.services.context_budget` exits **0** with all ten checks executed and passing.
`tests/test_far_field_gate.py`, `tests/test_query_endpoint.py`,
`tests/test_vector_search_stability.py`, `tests/test_provenance.py`, and
`tests/test_budget_counter_default.py` all pass. `pre-commit run --all-files` — ruff, ruff-format,
gitleaks, mypy (strict) — all pass.

The two corpus worst-case asserts in `run_live`, re-measured under this run against
`usable_budget` 193,488: `HeuristicCharCounter` worst case **675** tokens (286.6x slack, headroom
192,813); `TiktokenCounter` worst case **584** tokens (331.3x slack, headroom 192,904). Chunk 8.4
flagged both as passing with slack so large they cannot realistically fail — a chunk is capped at
1,999 characters, so no corpus satisfying the `CORPUS_MAX_CHUNK_CHARS` assert can fail these. That
observation stands unchanged; **NOT ESTABLISHED** whether either should be replaced by a tighter
invariant, and neither was touched here.

**The gate was not touched.** `app/services/evaluation.py` (`GATE_FIGURES`, the 12 figures) and
`app/services/retrieval.py` (`_PRE_RERANK_DIGEST`) are unmodified, as are
`app/services/vector_store.py`, `app/api/v1/query.py`, `app/schemas/query.py`, and everything under
`docs/`. Neither figure set is reachable from `_check_offline`: it returns `None`, writes no file,
prints nothing, and `app/services/evaluation.py` does not import `app/services/context_budget.py`
at all.

---

## 2026-09-09 — Chunk 8.3: `PER_CHUNK_OVERHEAD_TOKENS = 96` RECONCILED as a DELIBERATE OVER-RESERVE against a MEASURED 35-token ceiling; the `ProvenanceHeaderUnmeasured` tripwire is DELETED

> **Line-citation convention for this entry.** Every path below is repo-root-relative and every
> citation is pinned to a named revision: source and `docs/DECISIONS.md` citations to **`1af392e`**,
> `09_Memory/DECISIONS.md` citations to **`f1787a8`** — the revisions immediately before this entry
> was written. Prepending an entry shifts every line number beneath it, so an unpinned self-citation
> is stale the moment it is committed. Files are cited by full path throughout:
> `app/api/v1/query.py` and `app/schemas/query.py` share a basename, and a bare `query.py` would be
> ambiguous between them.

**Decision, in one line.** `PER_CHUNK_OVERHEAD_TOKENS` keeps the value 96, its status changes from
UNVALIDATED ESTIMATE / PROVENANCE NOT ESTABLISHED to **DELIBERATE OVER-RESERVE with a MEASURED
CEILING of 35 tokens**, and the tripwire that had been holding `app/services/context_budget.py`'s
`_demo()` red since chunk 6 — `ProvenanceHeaderUnmeasured` and
`_check_provenance_header_pending` — is deleted, because the condition it was written to enforce
is now met.

---

### A. The reconciliation: 96 retained, 35 measured, and they are not the same measurement

**The ceiling is 35 tokens.** Measured 2026-09-09 by `scripts/validate_token_counter.py` run
WITHOUT `--skip-headers`, against `app/services/provenance.py`'s own `render_header`/`render_block`
— the script imports them rather than reconstructing a format, so what was measured is the format
the generator will actually see. Measured at the frozen corpus's widest live values (`page=247`,
`chars=0-271256`), against an empty body, so the figure is the wrapper alone: header, its newline,
and the block separator.

**It is an UPPER BOUND, not a typical value.** Every other chunk in the corpus renders a shorter
header. 35 is what the widest possible header costs.

**96 is RETAINED, NOT CORRECTED, and the gap is not a discrepancy to reconcile — the two numbers
describe DIFFERENT FORMATS.** 96 was never measured against any format present in this tree. The
`23/40/49/69/74` header-token figures once cited as its basis return exactly one commit under
`git log --all -S` — `d7483ed`, the commit that wrote the claim — and no deleted script stands
behind it (established by the 2026-09-04 entry below, and unchanged by this one). Even the closest
thing to a measurement, the pre-8.2 reconstruction inside `scripts/validate_token_counter.py`,
passed a **chunk** id under the `doc_id=` label and so measured a wider string than the renderer
emits. **96's origin remains NOT ESTABLISHED and always will be.** What changed is that it is now
retained for what it does, not for where it came from.

**Why retain rather than lower to 35.** Over-reserving cannot overflow the context window;
under-reserving can. The 61-token difference buys margin against a format change, a wider corpus,
or a generation-model tokenizer that fragments the header harder than cl100k_base does — and it
costs 61 tokens per included chunk out of a 193,488-token usable budget. Lowering it to make the
two numbers match would trade real headroom for tidiness. **NOT ESTABLISHED:** whether 61 tokens is
the *right* margin. No analysis sized it; it is what fell out of retaining 96, and this entry does
not claim otherwise.

**ASSUMPTION, recorded because it is not verified.** The 35 is a wrapper-alone measurement —
`count(render_block(header, ""))` — which treats tokenization as **additive across the header/text
boundary**. A real block's `block_tokens - text_tokens` could differ if a subword merged across
that boundary. Not measured either way. The figure should not be read as tighter than it is.

**PINNED TO THE FROZEN CORPUS.** 35 is measured against `doc_01M0D39WZDYY7STA3PHWT5R4C7`, 260
chunks. Widest live values are corpus properties, not constants: **if the corpus unfreezes, this
measurement VOIDS** and `scripts/validate_token_counter.py` must be re-run before the figure is
relied on again.

Recorded in the source as a rewritten comment block above the constant in
`app/services/context_budget.py`. **The value itself was not edited.**

---

### B. The `HeuristicCharCounter` under-count: already recorded, and CLOSED — not an open item absorbed by this margin

Chunk 8.3's brief asked for this to be recorded as *"a live overflow path currently absorbed by the
`PER_CHUNK_OVERHEAD_TOKENS` over-reserve; OPEN, not fixed."* **That framing was not written,
because the tree contradicts it and this log already says so twice.** Recorded here as a
correction to the instruction rather than silently complied with or silently dropped.

**What is true:** `HeuristicCharCounter` under-counts against tiktoken cl100k_base — min margin
**0.9722x**, 4 of 260 chunks, worst offender `chk_01M0D4BMG6AASVJBVVD32X3B4S` at heuristic 105 vs
reference 108. Re-measured unchanged on 2026-09-09.

**Why it is not a live overflow path.** `HeuristicCharCounter` was **demoted** on 2026-08-25
(chunk 7 Phase E, entry below): `app/services/context_budget.py`'s `plan_context` defaults to
`TiktokenCounter`, which counts the real text and multiplies by `CROSS_TOKENIZER_SAFETY_FACTOR =
1.2`. The heuristic's error never reaches the budget unless a caller explicitly passes that
counter, and such a caller is choosing a known-unsafe estimate. The exposure was closed **by
demotion**, not by a margin.

**And it could not be absorbed by this margin in any case.** `PER_CHUNK_OVERHEAD_TOKENS` is a
fixed per-chunk token count covering the header format; the under-count is a *percentage* error on
chunk text. A constant cannot bound a proportional error — on a large enough chunk it is exceeded
by construction.

Recorded in the source at `HeuristicCharCounter`'s docstring in
`app/services/context_budget.py`, which already carried the 0.9722x figure and the 4-of-260 count.
Two things were added: the worst offender's identity, and an explicit STATUS line saying the item
is closed-by-demotion and is **not** absorbed by the over-reserve. **The counter was not fixed and
was not meant to be.**

---

### C. The tripwire is deleted, and why deleting it was safe only after A and B

Three deletions in `app/services/context_budget.py`, exactly as scoped by the chunk 8.2 report and
by the tripwire's own instructions:

1. the call site at the end of `_demo()`, including its two comment lines;
2. `_check_provenance_header_pending`, the function;
3. `ProvenanceHeaderUnmeasured`, the exception class.

Nothing else was deleted. `git diff --stat` for the whole chunk is one file: 62 insertions, 63
deletions.

**The tripwire's own closing condition, quoted from the code it was written in, was met exactly:**
"Build chunk 7's renderer, run `uv run python -m scripts.validate_token_counter` WITHOUT
`--skip-headers` to measure its real formats against `PER_CHUNK_OVERHEAD_TOKENS`, then delete this
check and its call site." The renderer is `app/services/provenance.py`, built in chunk 8.2
(`1af392e`). The measurement ran. This entry is the record.

**Why the ordering mattered, and why C would have been wrong on its own.** The tripwire's purpose
was never "block until a renderer exists" — it was "do not let an unsourced number stand
unexamined." Deleting it first would have removed the alarm while leaving the condition it was
alarming about undocumented, which converts a loud, greppable defect into a silent one. Phase A had
to land first so the constant carries its real status, and Phase B had to land so the one adjacent
measurement that looks like a reason to keep a margin is correctly attributed instead of being
folded into this decision as a rationale it does not support. **Deleting a tripwire is only safe
once the thing it was pointing at is written down somewhere that does not depend on the tripwire
existing.** That place is this entry and the two comment blocks.

**What replaces it as the ongoing guard.** `scripts/validate_token_counter.py`'s header section now
asserts `overhead <= PER_CHUNK_OVERHEAD_TOKENS` against the **real renderer**, so a format change
that outgrows the budget fails a run rather than a reconstruction. That assertion is live and
measures 35 against 96 today.

**Historical references retained.** `ProvenanceHeaderUnmeasured` still appears at five places in
`docs/DECISIONS.md` (`1af392e`) — the 2026-08-25, 2026-09-04, and earlier entries that describe it
while it existed. Those are historical records of what was true when written and are **left in
place, not rewritten**, per this log's append-only rule. The symbol is gone from the source; the
history of it is not.

---

### Verification (all run at the tree this entry describes)

`tests/test_far_field_gate.py`, `tests/test_query_endpoint.py`,
`tests/test_vector_search_stability.py`, `tests/test_provenance.py` — all pass, exit 0.
`.venv/bin/pre-commit run --all-files` — ruff, ruff-format, gitleaks, mypy (strict) all Passed.
`scripts/validate_token_counter.py` — still reports 35 vs 96, and no longer fails on a deleted
check.

**`python -m app.services.context_budget` now completes, exit 0** — the first clean `_demo()` run
since chunk 6 wrote the tripwire. Both invariants hold: `HeuristicCharCounter` worst-case hit 675
tokens (historical), `TiktokenCounter` worst-case hit 584 tokens against a usable budget of
193,488.

**The gate was not touched, by construction.** `app/services/evaluation.py` (`GATE_FIGURES`, the 12
figures) and `app/services/retrieval.py` (`_PRE_RERANK_DIGEST`) are **unmodified** — `git status`
shows exactly one changed file, `app/services/context_budget.py`. Neither figure set is on that
file's write path.

---

## 2026-09-09 — Chunk 8.0 decision record: NEVER WRITTEN. Reconstruction refused; tree state recorded as OBSERVATION only.

> **Line-citation convention for this entry.** Every path below is repo-root-relative and every
> citation is pinned to a named revision: source and `docs/DECISIONS.md` citations to **`746ac15`**,
> `09_Memory/DECISIONS.md` citations to **`7fa61b4`** — the revisions immediately before this entry
> was written. Prepending an entry shifts every line number beneath it, so an unpinned self-citation
> is stale the moment it is committed.

**Status.** No DECISIONS entry was written for chunk 8.0 at the time, and Asaad does not recall what
was decided. Intent that was never recorded is not reconstructed here: a rationale inferred from
commit subjects and shipped code has no source, and a later reader cannot tell it apart from one that
was actually decided. This entry keeps three things separate — the gap itself, what the tree verifies
as OBSERVATION, and the rationale that was recorded elsewhere and survives.

---

### 1. What the tree shows (OBSERVATION)

Each item verified directly at `746ac15`, not recalled.

- **The wiring commit.** `68707ed` — "feat: add POST /api/v1/query gated by the Guardrail". Six
  files, +417/-1. The gate is called at `app/api/v1/query.py:83`.
- **The shipped response.** `QueryResponse` carries four fields at `app/schemas/query.py:83-86`:
  `verdict`, `verdict_meaning`, `top_1_distance`, `citations`.
- **Citations on both verdicts.** Built unconditionally from `result.hits` at
  `app/api/v1/query.py:92-104`, with no branch on verdict, so `ABSTAIN_OUT_OF_DOMAIN` carries them
  too.
- **No `answer` field.** It does not exist in the schema.
- **No generation on the route.** No generation call exists anywhere in `app/`. `llm_api_key` is
  configured but read only by the readiness probe at `app/api/v1/health.py:55`, for presence.
- **No auth, no rate limiting.** Neither exists on the route or in middleware.

### 2. Rationale that WAS recorded — not reconstruction

The premise that chunk 8.0 left no rationale was tested against the tree and is **false**. None of it
reached DECISIONS, but it was written down at the time and it survives in committed prose:

- `68707ed`'s commit body records why there is no `answer` field, why citations are returned on both
  verdicts and both are 200, why `MAX_QUESTION_CHARS` is derived by import rather than chosen, why
  `rrf_score` is excluded from the payload while `rerank_score` and `vector_distance` are exposed,
  why `GuardrailInputError` and `CorpusUnavailableError` return 503 rather than 500, why `arm` and
  the `k` values are not exposed, and why auth, rate limiting and CORS are absent.
- `app/schemas/query.py:1-8` restates the no-`answer` reasoning; the `QueryResponse` docstring at
  `app/schemas/query.py:73-82` records why `verdict` is the first field and why a refusal carries
  its citations.
- `app/api/v1/query.py:1-10` names auth, rate limiting and CORS as required before any deployment
  and assigns that policy to Step 04, citing `app/core/security.py`.

Reading these is not inference. They are the decisions' own contemporaneous statements; what is
missing is the DECISIONS entry, not the reasoning.

### 3. What is NOT ESTABLISHED

Two choices have no recorded rationale anywhere in the tree, and none is invented here:

- Why the far-field gate is the **sole** ship gate on the route — no groundedness check and no other
  verdict source exists.
- Why `top_1_distance` is surfaced at the top level of the response. `68707ed` explains which
  *citation* scores are exposed and why; it is silent on this one.

---

**Lesson.** A decision entry written after the fact is bounded by memory. What saved most of chunk
8.0 was not recall but the habit of writing the reasoning into the commit body and the module
docstring while the decision was being made — the two items now unrecoverable are exactly the two
nobody wrote down anywhere. Write the entry in the chunk that makes the decision; until then, the
commit body is the backstop.

---

## 2026-09-05 — Chunk 8.1 scope line "apply API Contract 1.1.0": CLOSED. Amendment remains approved and pending.

> **Line-citation convention for this entry.** Every path below is repo-root-relative and every
> citation is pinned to a named revision: source and `docs/DECISIONS.md` citations to **`2feacdd`**,
> `09_Memory/DECISIONS.md` citations to **`cef64e8`** — the revisions immediately before this entry
> was written. Prepending an entry shifts every line number beneath it, so an unpinned self-citation
> is stale the moment it is committed.

**Disposition.** The scope line is closed. The amendment is not promoted, folded, or dropped. It
remains approved and pending, unapplied. DROP was rejected because it would strand the live Step 04
`citations[].score` constraint.

---

### 1. The scope line named a non-existent document

"API Contract 1.1.0" does not exist. `API Contract.md` exists at v1.0.0 and defines §5.1. Prior
entries recording "1.1.0 does not exist" were correct but blurred the two.

### 2. Amendment location

Byte-identical in both trees at `docs/DECISIONS.md:2839-2872` as of `2feacdd` and
`09_Memory/DECISIONS.md:2826-2859` as of `cef64e8`. Approval by Asaad 2026-08-21 rests solely on the
amendment's own first sentence — no independent corroboration. **NOT ESTABLISHED.**

### 3. The trigger is stated, not ambiguous

**SUPERSEDES** the earlier finding that it was "genuinely ambiguous, unresolved." The amendment's
status paragraph states the contract must be edited when the reranker is wired into the response
path. The earlier reading took the trigger from the rationale paragraph ("the answer still ships")
instead of the status paragraph.

### 4. NOT FIRED — OBSERVED

§5.1 is the eleven-field message object (`id`, `conversation_id`, `role`, `content`, `status`,
`citations`, `trust`, `guardrail`, `plan`, `timings_ms`, `created_at`). The shipped route returns the
four-field `QueryResponse` at `app/schemas/query.py:73` (`verdict`, `verdict_meaning`,
`top_1_distance`, `citations`), sharing only `citations`.

Neither 1.1.0 item exists: `Retrieval.rerank` is populated at `app/services/retrieval.py:460` and
discarded by the serving layer; `degraded` has no producer because `RerankError` is deliberately
uncaught, stated at `app/services/retrieval.py:379`. The sole reranker field reaching the wire is
`rerank_score`, serialized at `app/api/v1/query.py:102` onto `CitationOut` (declared at
`app/schemas/query.py:70`); the amendment's own version table assigns `citations[].score` version
none.

### 5. Testable trigger definition (deliverable)

The trigger fires when the §5.1 message object is served with `timings_ms.rerank` populated and a
`degraded` array present. `citations[].score` alone does not fire it, per the amendment's own version
table. Until then the amendment cannot be applied.

### 6. Also recorded

The amendment is silent on §5.3 streaming. The changelog rule at `API Contract.md:16-17` defines
"breaking" only; "so a minor bump" imports semver the document never establishes. Two of the rule's
three obligations remain unmet.

---

**Provenance.** A pass report was reported at `/tmp/chunk81d/contract-amendment.md`. That directory
exists and is empty. `grep -rln` over the vault's
`/home/asaad/Documents/DevBrain/rag-reliability/passes/` and over `/tmp/` returns no copy of the
report. Every hit is one of three kinds: the five surviving chunk-8.1 artifacts and prior-session
scratch copies, both matching on the decision heading; or drafts of this entry, which match only
because this sentence contains the path string it searches for. The total is not recorded here — it
drifts with any scratch file written under `/tmp/`. **NOT RECOVERABLE.** This entry rests on direct
reads of the tree at `2feacdd`, not on that report.

**Lesson (second instance this session).** A pass report is not a durable citation. First instance:
`ea70871` §7 asserted the `messages.guardrail` sense "is not established" after retiring the
secondary source without re-reading the primary. Here the trigger was called ambiguous from a summary
while the amendment's own status paragraph was quotable.

**Lesson (third instance).** A bare filename is not a citation when the repo holds two files of that
name — `app/api/v1/query.py` and `app/schemas/query.py`. Paths in this entry are repo-root-relative.

---

## 2026-09-05 — `messages.guardrail` records the `injection_scanner` sense: SUPERSEDES the `ea70871` §7 claim that its sense is not established; column LEFT IN PLACE, no migration

> **Line-citation convention for this entry.** Every `docs/DECISIONS.md:N` below is pinned to
> **`7f096e3`**, the revision immediately before this entry was written. Prepending an entry shifts
> every line number beneath it, so an unpinned self-citation is stale the moment it is committed —
> the same discipline `ea70871` used, and the reason its own `:2224` pointer needed re-resolving
> (see CITATION DRIFT below).

**Decision:** `messages.guardrail` is **left in place**. **No migration.** The column records the
**`injection_scanner`** sense and is correctly shaped for it. It is **unbuilt, not dead** —
`app/agents/` is empty because that agent is planned, not abandoned.

**OBSERVED** — "dead" and "not yet built" have the same grep signature and different dispositions.
`README.md:56` marks the stage *"Planned, not implemented"* and `README.md:367` records `agents/` as
*"empty"*. Neither says abandoned.

---

### 1. SUPERSEDES A FALSE CLAIM

`docs/DECISIONS.md:336-341` as of `7f096e3` — the `ea70871` §7 DEFERRED item — states:

> with three senses retired into three names, it is not
> established which one the column was meant to record.

**That is OBSERVED FALSE.** The sense is establishable from primary sources and is established in
§2 below.

**The `ea70871` entry is NOT edited.** This entry supersedes that one claim and nothing else. Under
this log's own convention (`docs/DECISIONS.md:1-9` at `7f096e3`) superseded entries are annotated or
superseded, never rewritten.

**Its other claim stands, OBSERVED, reproduced exactly this pass:**

> **OBSERVED:** no application
> code reads or writes it, so deferring costs nothing at runtime.

Four commands establish that absence. The decisive one: `grep -rn "Message(" app scripts tests
--include="*.py"` returns only `app/db/models/message.py:23`, the `class Message(Base)` definition
itself — **the model is never instantiated anywhere in the codebase.** `Message` is imported only by
`app/db/models/__init__.py:11`, its own package re-export. No raw SQL names the table, no fixture or
seed writes it, and `QueryResponse` (`app/schemas/query.py:73`) does not carry the field.

---

### 2. BASIS — six primary sources, the name excluded from the reasoning

The column's name is exactly what was in question, so no link below relies on it. Every link is a
payload definition or a field binding.

**OBSERVED.** `app/db/models/message.py:1-7` binds the column to the contract:

> ```
> """`messages` — API Contract §5.1.
>
> `citations`, `trust`, `guardrail`, `plan`, and `timings_ms` are JSONB rather than
> five relational tables: the contract already defines their shape as nested JSON,
> nothing in the app queries inside them, and Step 03/04 own populating them.
> ```

The question "which sense does the column record" therefore reduces to "what does §5.1's
`guardrail` object contain".

**OBSERVED.** `01_Projects/RAG Reliability System/API Contract.md:209-217` defines that object:

> ```
> "guardrail": {
>   "status": "pass",
>   "risk_score": 0.04,
>   "checks": [
>     { "id": "LLM01_prompt_injection", "target": "query", "result": "pass", "detail": null },
>     { "id": "LLM01_prompt_injection", "target": "retrieved_context", "result": "pass", "detail": null },
>     { "id": "LLM02_sensitive_disclosure", "target": "output", "result": "pass", "detail": null }
>   ]
> },
> ```

`LLM01` / `LLM02` are OWASP LLM Top 10 identifiers — `03_Resources/OWASP LLM Top 10.md:26-27`.

**OBSERVED — the discriminating fact.** `API Contract.md:241` gives the enum:

> `` `guardrail.status`: `pass` · `flagged` (answered, surfaced as a warning) · `blocked`. ``

The shipped `far_field_gate()` returns `ANSWER` / `ABSTAIN_OUT_OF_DOMAIN` / `ANSWER_UNVERIFIED`.
**No value overlaps.** A far-field verdict cannot occupy this field without violating the contract.
This rules the `far_field_gate` sense out on shape alone, independently of intent.

**OBSERVED.** `01_Projects/RAG Reliability System/Step 03 — Agents.md:105` binds the payload to the
same array — *"Map each check to an OWASP LLM ID; return the `checks[]` array from the contract"* —
and `:102-104` scope it to query, retrieved-chunk, and output scanning. Not one line mentions
distance, abstention, or out-of-domain.

**OBSERVED, corroborating.** `_Overview.md:37` — *"Prompt-injection scan on query _and_ retrieved
chunks, OWASP LLM checks, PII"*. `API Contract.md:235` and `:314` tie `refused_blocked` to
*"injection check `fail`"*, and `app/db/models/message.py:32` carries that same status enum.

**INFERRED** — that no revision since 2026-08-18 changed the intent. The contract is still
`version: 1.0.0` and the pending 1.1.0 amendment does not touch this field.

**NOT ESTABLISHED** — the same question for `timings_ms.guardrail` (`API Contract.md:222`), a
separate key in a different column, and for the SSE `event: guardrail` (`:258`, `:272`). Both carry
the retired bare name. Neither was in scope, and neither is covered by the `docs/DECISIONS.md:335-341`
deferral list at `7f096e3`.

---

### 3. METHODOLOGY DEFECT — how the gap arose, and the standing note it produces

**OBSERVED.** `docs/DECISIONS.md:225-226` at `7f096e3` records that the `ea70871` pass retired a
secondary source:

> `naming-and-contract.md` is retired as a source and is not cited
> by this entry for any figure.

**OBSERVED.** That retired document carried the contract's payload shape —
`rag-reliability/passes/chunk81-artifacts/naming-and-contract.md:209-213`, *"the `guardrail`
response object with `LLM01_prompt_injection` checks"*.

**INFERRED.** Retiring the secondary source dropped the finding with it, and `API Contract.md` was
not re-read directly. This is a reconstruction of how the gap arose, not an observation of intent.

**STANDING METHODOLOGY NOTE — retiring a bad secondary source must trigger a read of the primary,
not leave a hole.** A retired source's *citations* are suspect; the *facts it pointed at* are not
retired with it. Discarding both is how a pass converts a citation defect into a false "not
established". Applies to any future source retirement in this project.

**Related correction, OBSERVED.** Earlier passes recorded that **"API Contract 1.1.0 does not
exist"**. That remains **true** — `docs/DECISIONS.md:231-234` at `7f096e3` establishes the contract
is v1.0.0 and 1.1.0 exists only as an approved-unapplied amendment. But `API Contract.md` **itself
does exist**, at v1.0.0, and **does define §5.1** including the `guardrail` object quoted above.
Prior phrasing blurred "version 1.1.0 does not exist" with "the contract does not define this
field". The first is true; the second is false.

---

### 4. OBSERVED STATE

**Schema.** `alembic/versions/0002_pipeline_core_users_documents_chunks_.py:166`:

> ```
> sa.Column("guardrail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
> ```

**Verified against the live catalog, not read off the migration** (`information_schema.columns`,
`pg_constraint`, `pg_indexes`): type `jsonb`, nullable `YES`, **no default, no check constraint, no
index**. The table's only constraints are `messages_pkey` and
`messages_conversation_id_fkey`; its only indexes are the pkey and `ix_messages_conversation_id`.
Nothing at the schema level constrains the contents.

**Migration state.** `uv run alembic current` → `0002 (head)`; `uv run alembic heads` → `0002
(head)`. Applied and head, no later revision. `downgrade()` drops the whole table; there is no
column-level down path.

**Data.** Database reachable, so this is measured, not inferred from the absent write paths:

> ```sql
> SELECT
>   (SELECT count(*) FROM messages)                             AS total_rows,
>   (SELECT count(*) FROM messages WHERE guardrail IS NOT NULL) AS guardrail_non_null
> ```
>
> `total_rows = 0   guardrail_non_null = 0`

**NOT ESTABLISHED** — whether it has *ever* held data. A count of 0 describes the present state of
one database. `messages` has no audit trail (`audit_events` is written only by the ingestion path,
`app/services/ingestion.py:27`), so an inserted-then-deleted row would leave no trace this pass
could find. Emptiness was **not** inferred from the absence of write paths.

---

### 5. DISPOSITIONS NOT TAKEN

**Rename** (to match the sense): needs a new `0003`; **breaking** by the contract's own rule, quoted
at `docs/DECISIONS.md:2632` at `7f096e3` — *"Changing a field name, status enum, or error code is a
breaking change."* That is a **major** bump, 1.0.0 → 2.0.0, which the pending 1.1.0 amendment does
not contemplate. Data migration would be trivial (0 rows), but the versioning event is not.

**Drop** (as dead): needs a new `0003`; **breaking**, and it **contradicts an approved spec line** —
`Step 03 — Agents.md:138`, *"Every response carries `plan`, `guardrail`, `trust`, `citations`,
`timings_ms` exactly as in [[API Contract]] §5.1."* Also mis-founded: per the Decision above the
column is unbuilt, not dead.

**OBSERVED** — both rejected options are runtime-free (0 rows, 0 code paths). Neither was rejected
for risk of breakage; both were rejected for being contract events with no offsetting benefit.

---

### 6. CITATION DRIFT

**OBSERVED.** The `:2224` reference to the 1.1.0 amendment has drifted, twice. It is now
`09_Memory/DECISIONS.md:2610` = `docs/DECISIONS.md:2623` at `7f096e3`, and `ea70871`'s own
cross-reference (`docs/DECISIONS.md:236-237` at `7f096e3`) cites the earlier pair
*"`09_Memory/DECISIONS.md:2224` (= repo `docs/DECISIONS.md:2237`)"*. One entry, three line numbers,
all correct at their own moment.

**The cause is structural, not clerical:** this log is newest-first, so every prepended entry shifts
every line beneath it. **Cite line numbers in this file with a revision pin, as `ea70871` did and as
this entry's header does, or they go stale on write.** Vault line numbers drift independently of
repo ones — the two files carry different preambles — so a vault citation needs its own qualifier.

---

### 7. CARRIED, NOT RESOLVED

**OBSERVED**, none acted on:

1. `README.md:31-33` fuses both senses in one paragraph — *"inspects retrieved content before it
   reaches the model"* (scanner) and *"where a query with no adequate supporting evidence is stopped
   rather than answered"* (far-field). Already carried at `docs/DECISIONS.md:344-347` at `7f096e3`.
2. `README.md:56` marks the stage *"Planned, not implemented"* while the far-field half ships.
3. `timings_ms.guardrail` (`API Contract.md:222`) and the SSE `event: guardrail` (`:258`, `:272`) —
   the retired bare name in a different column and a different channel, on no deferral list.
4. The 1.1.0 amendment remains APPROVED, NOT YET APPLIED. Any §5.1 change sequences against it.

---

## 2026-09-04 — the "MEASURED" claim on `PER_CHUNK_OVERHEAD_TOKENS` is RETIRED as OBSERVED FALSE: the value keeps 96, its provenance is NOT ESTABLISHED, and the tripwire's own literals are de-drifted

**Decision:** `PER_CHUNK_OVERHEAD_TOKENS` keeps its value of **96**. The MEASURED claim on it is
**retired**. The value is labelled **UNVALIDATED ESTIMATE**, provenance **NOT ESTABLISHED**. The
closing condition is a real measurement via `scripts/validate_token_counter.py` run WITHOUT
`--skip-headers`, which is **blocked on the renderer's absence**.

**OBSERVED** — the value is unchanged at `app/services/context_budget.py:256`; the diff carries that
line as context, never as a `-`/`+` line.
**INFERRED** — that 96 is a *safe* estimate. Nothing in this pass measured it. Retiring the claim
says only that it was never measured, not that it is wrong.

---

### 1. FINDING — the value never had a derivation

**OBSERVED.** No script, command, or recorded derivation anywhere in this repo produces 96, or the
23/40/49/69/74 header-token figures cited as its basis. `git log --all -S` on both figure strings
(`"69, and 74"` and `"23, 40, 49"`) returns exactly one commit, **d7483ed** — the commit that wrote
the claim. `git log --all --diff-filter=D --name-only` finds no deleted script behind it. The claim
existed in four places, all prose: the source comment, the chunk 6 entry's table, the `d7483ed`
commit message, and the vault mirror. No artifact in any of them.

**OBSERVED.** The 2026-08-24 (correction) entry — at `docs/DECISIONS.md:1653-1660` as of `ea70871`,
before this entry was prepended — retracted **a claim, not a result**: *"A number described as
measured, where the artifact being measured was never built and 3 of 5 inputs were never written
down, is not a measurement."* It explicitly left the value untouched: *"nothing in this correction
says 96 is wrong, only that the claim it was measured is false."*

**OBSERVED.** That correction landed in `DECISIONS.md` and in the runtime guard. It did **not** land
in the source comment. The false MEASURED comment therefore survived a correction aimed at something
else, and sat 593 lines above a guard in the same file asserting the opposite. Retired here.

**NOT ESTABLISHED** — where 96 actually came from. It cannot be traced to a command, a script, or a
recorded derivation, and this entry does not infer an origin from the comment that claimed one.

---

### 2. FINDING — a second defect, independent of the first

**OBSERVED.** The guard's `96` was a **string literal**, and four `_demo()` fixtures passed `96` as
literals. The tripwire could therefore drift off the very constant it guards: changing the constant
would have left the guard still printing the old number.

All five sites now derive from `PER_CHUNK_OVERHEAD_TOKENS`, plus the manifest round-trip assert at
`:784` — same function, same drift class, flagged in the read-only pass and fixed with it rather
than left as a known gap of the exact kind being closed.

**OBSERVED.** `grep -n "96"` over the whole file now returns exactly one line: `256:
PER_CHUNK_OVERHEAD_TOKENS = 96`, the constant itself.

**OBSERVED.** The fixtures' *expectations* were de-drifted alongside their budgets. Pinning the
fixture while leaving `496` / `992` / `8` / `"400 text"` hardcoded would have replaced one drift gap
with a worse one — a fixture that floats against expectations that do not. `big_tokens` derives from
`counter.count()` rather than re-deriving `CHARS_PER_TOKEN`.

**OBSERVED.** The guard docstring could not interpolate a constant. The number was removed from its
prose rather than performing `__doc__` surgery to make a docstring compute.

---

### 3. BLAST RADIUS

**OBSERVED.** `app/services/evaluation.py` and `app/services/retrieval.py` do not import
`app.services.context_budget` at all. The 12-figure gate, `_PRE_RERANK_DIGEST`, and the class-1 miss
set are unaffected by this constant and by this change.

**OBSERVED.** `usable_budget` (193,488) does not subtract the per-chunk overhead — it is pure
reserve arithmetic over the other four values, so it is arithmetically independent of 96.

**OBSERVED.** There is exactly one behavioural read: `plan_context` at `:543`. It is **unchanged** —
the diff produces no hunk anywhere in that function body, jumping from the guard docstring straight
to `_check_offline`.

**OBSERVED.** One frozen artifact records the value: `docs/chunk7-scores.json:32`, written by
`budget_manifest()` and read back by nothing.

---

### 4. TRIPWIRE — intact, and its output difference is not a regression

**OBSERVED.** `_demo()` still exits **1** with `ProvenanceHeaderUnmeasured`. stdout is
**byte-identical** to clean `HEAD`. stderr differs in **three lines only**, all traceback frame line
numbers: `926->953`, `922->949`, `828->854`. That shift is arithmetic — +27 lines added above the
raise — not behavioural.

**OBSERVED.** The exception message is **byte-identical** (`cmp` on the final stderr line: equal),
and is now *derived* from the constant rather than hardcoded.

**This must not be read as a regression.** The tripwire is a deliberate blocking prerequisite for
chunk 7 and was neither suppressed nor repaired. It stays red until chunk 7's renderer exists and
its real header formats have been measured.

---

### 5. EXECUTION

**OBSERVED.** One file, `app/services/context_budget.py`, **59 insertions / 32 deletions**. No other
file in repo or vault was modified by the code change.

**OBSERVED.** One reflow was needed: f-stringifying the guard message pushed a line to 106 characters
against `line-length = 100`. Split across two string literals; the message text is unaffected, as the
byte-identical comparison above confirms.

---

### 6. VERIFICATION — all PASS

**OBSERVED**, each with raw output captured this pass:

| check | result |
|---|---|
| `_PRE_RERANK_DIGEST` reproduces | PASS — exit 0, pinned baseline, lineage `5db0925`, 8 questions x 2 arms |
| 12-figure gate, demo | PASS — exit 0 |
| 12-figure gate, live CLI | PASS — lexical `0.233 0.400 0.567 0.142`, vector `0.833 0.900 1.000 0.700`, hybrid `0.667 0.867 0.967 0.445`; all 12 exact |
| class-1 miss set | PASS — the 3 recorded misses reproduce: `ood08 1.4131`, `ood01 1.4898`, `ood10 1.4906`, all ANSWER_UNVERIFIED as recorded (`bc1e261`) |
| `python -m tests.test_far_field_gate` | PASS — exit 0 |
| `python -m tests.test_query_endpoint` | PASS — exit 0 |
| `python -m tests.test_vector_search_stability` | PASS — exit 0, 36/36 bitwise-identical across 12 processes |
| `.venv/bin/pre-commit run --all-files` | PASS — ruff, ruff format, gitleaks, mypy(app) |
| `mypy --strict app scripts tests` | PASS — no issues in 57 source files |
| `_demo()` tripwire | PASS — exit 1, `ProvenanceHeaderUnmeasured`, output diffed vs clean HEAD |

---

### 7. CARRIED, NOT RESOLVED

**OBSERVED**, both found during this pass, neither acted on:

1. `docs/chunk5-results.json` lacks the `per_chunk_overhead_tokens` manifest key despite
   `scripts/chunk5_benchmark.py:206` emitting `budget_manifest(...)` into its manifest.
2. `09_Memory/SCRATCHPAD.md` has no chunk-7 prerequisites despite `docs/DECISIONS.md:1896` stating
   they are "listed in SCRATCHPAD".

---

## 2026-09-04 — the bare name `guardrail` is RETIRED repo-wide: three unrelated senses, three names; the DB column, the architecture prose and every prior entry are UNCHANGED

**Decision:** the bare name `guardrail` is **retired**. It was carrying three
unrelated mechanisms, and no sense keeps it.

| sense | new name | status |
|---|---|---|
| far-field top-1 distance abstain cut | `far_field_gate` / `FAR_FIELD_ABSTAIN_DISTANCE` | **renamed this pass, shipped** |
| OWASP prompt-injection scanner | `injection_scanner` | **name reserved; not implemented** |
| reranker relevance-score threshold | `relevance_floor` | **renamed this pass, never implemented** |

**OBSERVED** — the three senses and their line assignments, from a fresh
`git grep -n -i -I guardrail` at `68707ed`: 127 matching lines across 20 files,
reproducing the prior inventory line for line.
**INFERRED** — that these are exactly three senses and not two or four. The
assignment of each individual line to a sense is a judgement, not a measurement.

---

### 1. BASIS — why no sense keeps the bare name

**OBSERVED.** The vault pass `/tmp/chunk81/sense-a-basis.md` was run to test whether
the OWASP sense holds a prior claim on the name that would let it keep the bare
form. It does not:

- **Parity, not priority.** Across the 107 working vault lines the OWASP scanner
  accounts for **34 (32%)** and the far-field cut for **33 (31%)**
  (`sense-a-basis.md:403-404`, `:501-502`). The two senses are level.
- **The two-day priority gap is NOT ESTABLISHED.** The claim that the OWASP sense
  predates the far-field sense by two days does not survive the pass.
- **The nominated earliest record is the wrong sense.** The line put forward as the
  OWASP sense's earliest record — `09_Memory/DECISIONS.md:2361`
  (= repo `docs/DECISIONS.md:2374`) — is **reranker-threshold** text
  ("…contaminate the Guardrail threshold fit downstream"), not the injection
  scanner (`sense-a-basis.md:248`, `:443`).

**INFERRED.** With no priority and no majority, there is no principled holder of the
bare name, so retiring it for all three is the only assignment that does not
silently privilege one sense over another.

---

### 2. DEFECT — `naming-and-contract.md` is RETIRED as a source

**OBSERVED.** Its sha256 **MATCHES** the pinned value
(`08135877b57d314bc7132e4b69d1c9937d5d26e7ec64a2b24871a56072ec2919`), so the bytes
are the bytes that were pinned. That is the whole of what the hash establishes.

**Four of its seven figures do not reproduce:**

| figure | claimed | this pass |
|---|---|---|
| total matching lines | 234 | **DIVERGES** |
| (a) far-field lineage | 167 | **DIVERGES** |
| (b) OWASP scanner | 49 | **DIVERGES** |
| (c) ambiguous / other | 18 | **DIVERGES** |
| mechanical cost, lines | ~60 | AGREES |
| Python files touched | 6 | AGREES |
| production call sites | 1 | AGREES |

**OBSERVED.** Its (a)/(b)/(c) split states **no assignment rule** — the reader is
given three buckets and no criterion for placing a line in one of them. It is
unmethodized hand classification presented as a count.

**OBSERVED.** Its 234 total is substantially **self-counting**: **127 of the 234 hits
are lines inside the document itself** (`sense-a-basis.md:518`). A report that
supplies over half its own evidence is measuring its own prose.

**NOT ESTABLISHED.** Whether its repo count was taken over a working tree that
already contained the report. If it was, the repo half is contaminated the same way.

**INFERRED.** Pinning a hash pins bytes, not accuracy. A matching digest over a
document whose figures do not reproduce is a reproducibility guarantee attached to
the wrong property. `naming-and-contract.md` is retired as a source and is not cited
by this entry for any figure.

---

### 3. DEFECT — API Contract 1.1.0 does not exist as a document

**OBSERVED.** The contract is **v1.0.0, dated 2026-08-18** — frontmatter reads
`version: 1.0.0`, the H1 reads `# API Contract v1.0.0`, and the changelog holds a
single row (`1.0.0 | 2026-08-18 | Initial contract`).

**OBSERVED.** "1.1.0" exists only as an **approved-but-unapplied amendment**, recorded
at `09_Memory/DECISIONS.md:2224` (= repo `docs/DECISIONS.md:2237`), *"API Contract
1.1.0 amendment: APPROVED, NOT YET APPLIED"*, whose own body states *"`API Contract.md`
still reads 1.0.0 and must be edited when the reranker is wired into the response
path."* It names `guardrail` once, inside a §5.1 field list, and **defines nothing
about it**.

**INFERRED.** The chunk 8.1 scope line *"apply API Contract 1.1.0"* was therefore
written against a document that is not there. Any work scoped to it is scoped to an
approval record, not a specification.

---

### 4. DEFECT — three line-citation drifts in `naming-and-contract.md`

**OBSERVED.** Three citations point one or two lines off target:

| cited | actual | quoted string |
|---|---|---|
| `Step 03 — Agents.md:96` | `:98` | "Emit `refused_no_context` when nothing clears the relevance floor" |
| `_Overview.md:39` | `:38` | "Low score → `partial` or `ungrounded` verdict" |
| the table range | shifted | — |

**OBSERVED.** Every quoted string checks out verbatim at the corrected line.
**INFERRED.** Off-by-small line drift, not fabrication — but it is a fourth
independent reason the document is not usable as a citation source without
re-verification.

---

### 5. SUPERSEDES — this entry does not edit anything

**OBSERVED.** Three pushed commit subjects name the retired symbol:
`68707ed` *"…gated by the Guardrail"*, `7dffb81` *"…add the Guardrail far-field
abstention…"*, `bc1e261` *"…pin unfitted far-field placeholder"* (body). All three are
reachable from `origin/main`. They **stand as written**; this entry supersedes them.

**OBSERVED.** The `bc1e261` entry (this file, the 2026-09-04 Chunk 7.5 entry, plus its
supersede annotation) names the retired symbol 11 times and is **unedited**.

**OBSERVED.** `DECISIONS.md` received **zero edits** in this pass. All 24 of its hits
are dated historical records of decisions made under the name in force at the time,
and the file has **no standing-policy section** — it is append-only dated entries,
newest first. Rewriting any of them would make the log claim a name was in use before
it existed.

---

### 6. EXECUTION RECORD

**OBSERVED.** 71 lines across 9 files; **71 insertions / 71 deletions** — a pure 1:1
substitution, no line added or removed anywhere. Every one of the 71 was checked
against its expected prior text **before** any write; **zero mismatches**.

**OBSERVED — the 6-line delta against the plan's 65 is established from the diff**,
by differencing the plan's edit list against `git diff -U0`. It is exactly:

```
app/services/context_budget.py:6, 274, 402, 418, 429, 739
```

the six lines the plan left **ambiguous** and a later amendment assigned to
`relevance_floor`. It is **not** the satellite renames (`FarFieldVerdict`,
`FarFieldDecision`, `FarFieldInputError`, `far_field_input_handler`) — those were
already inside the plan's 65.

**INFERRED.** Those six lines name the consumer of `BudgetedContext`. The far-field
gate is ruled out affirmatively — it takes a `Retrieval`, reads
`hits[0].vector_distance` and nothing else, and never receives a `BudgetedContext` —
and `context_budget.py:402` independently pins the survivor by naming the reranker
score outright.

**OBSERVED.** The plan predicted wrap edits at `app/core/errors.py:159` and
`app/services/retrieval.py:101`. **Neither was needed**; both lines are unmodified.
The renames absorbed into the existing wraps at 83 and 85 characters, under ruff's
100-character limit.

**OBSERVED.** `FarFieldVerdict` and `FarFieldDecision` are not a wrapper pair and were
renamed as-is with no refactor: the first is a `Literal` alias over the three verdict
strings, the second a frozen dataclass carrying a verdict **plus** `top1_distance`.

**OBSERVED.** The verdict strings `ANSWER` / `ABSTAIN_OUT_OF_DOMAIN` /
`ANSWER_UNVERIFIED` contain no case-form of the retired token and are **unchanged**.
They are the wire contract, they are pinned in the unamendable `bc1e261` entry, and
they name outcomes rather than mechanisms.

**OBSERVED — residue, 11 lines, all expected, none beyond:**

```
README.md:16, 31, 56, 367                    architecture prose, deferred
app/agents/__init__.py:1                     architecture prose, deferred
scripts/validate_token_counter.py:195        budget overflow, no sense fits
scripts/chunk7_scores.py:8, 296, 635, 767    quoted record + JSON emitters, frozen
scripts/chunk5_benchmark.py:290              JSON emitter, frozen
```

---

### 7. DEFERRED — not resolved by this entry

- **`messages.guardrail` JSONB column**, applied at alembic `0002` head
  (`alembic/versions/0002_pipeline_core_users_documents_chunks_.py:166`,
  `app/db/models/message.py:36`). Needs its own migration **and a decision on what the
  payload means** first — with three senses retired into three names, it is not
  established which one the column was meant to record. **OBSERVED:** no application
  code reads or writes it, so deferring costs nothing at runtime.
- **`app/agents/__init__.py:1` and `README.md:16, 31, 56, 367`** — architecture prose
  in which one pipeline stage fuses the injection and abstention jobs. Splitting it is
  an architecture decision, not a rename. **`README.md:31-33` remains a carried
  defect**, and `README.md:56` is now additionally stale: it marks the stage "Planned,
  not implemented" when the far-field half ships.
- **`scripts/chunk7_scores.py:8, 296, 635, 767` and `scripts/chunk5_benchmark.py:290`** —
  frozen. `:8` is a verbatim quotation of a historical DECISIONS entry; the other four
  are string literals that emit `docs/chunk7-scores.json` and `docs/chunk5-results.json`,
  so editing them would desynchronise the scripts from their recorded output.
- **`scripts/validate_token_counter.py:195`** — concerns budget overflow reaching the
  abstention stage. **None of the three senses covers it.** Left as found.
- **Corpus JSON** (`app/corpus/corpus_vectors.json`, 11 lines / 15 occurrences) —
  untouched by construction. The word appears in the ingested source document's own
  prose; the embeddings were computed from that text and are pinned by
  `vectors_sha256`.

---

### 8. VERIFICATION — all PASS

**OBSERVED.** Run after the edits, before the commit:

| check | result |
|---|---|
| residue grep | PASS — 11 lines, all expected |
| tight grep (`app/services app/api app/core app/schemas tests`) | PASS — empty |
| old-identifier grep | PASS — empty |
| `_PRE_RERANK_DIGEST` (8 questions × 2 arms, 560 hits) | PASS — byte-identical |
| 12-figure gate, `evaluation._demo()` | PASS |
| 12-figure gate, live `python -m app.cli evaluate` | PASS — all 12 exact |
| pinned class-1 miss set (ood08 1.4131, ood01 1.4898, ood10 1.4906) | PASS — all `ANSWER_UNVERIFIED` |
| `python -m tests.test_far_field_gate` | PASS |
| `python -m tests.test_query_endpoint` | PASS |
| `python -m tests.test_vector_search_stability` | PASS — 36/36 across 12 processes |
| `pre-commit run --all-files` | PASS |
| `mypy .` strict, 60 source files | PASS |

**OBSERVED.** Postgres was up and healthy (`rag-postgres-dev`, `127.0.0.1:5432`), so the
12-figure gate ran **for real** against the live corpus rather than being skipped. The
recorded figures reproduced exactly: lexical `0.233 0.400 0.567 0.142`, vector
`0.833 0.900 1.000 0.700`, hybrid `0.667 0.867 0.967 0.445`.

---

### 9. PRE-EXISTING — not caused by this pass

Both confirmed by stashing to clean `68707ed` and re-running. **OBSERVED:**

- **`app/services/context_budget.py` `_demo()` exits 1** with
  `ProvenanceHeaderUnmeasured`, a deliberate tripwire that says so in its own message
  (*"Build chunk 7's renderer … then delete this check"*). Identical exit code and
  message at clean HEAD, and the demo's full output is **byte-identical** before and
  after the edits.
- **`ruff format --check` flags `docs/PLAN-step03-chunk1-status.md`**, a markdown file
  containing Python code blocks that this pass never touched. Same single-file
  complaint at clean HEAD. `pre-commit`'s `ruff format` hook passes because it is
  scoped to Python files.

---

## 2026-09-04 — Chunk 7.5: the retrieval-side measurement line is CLOSED; classes 2–7 are NOT authored, and the Guardrail carries an UNFITTED far-field placeholder

**Decision:** the retrieval-side measurement line is **closed**. **Chunk 7.4
phase 1 stands as the final result of that line** — not as its first instalment.
**Classes 2–7 are NOT authored. Consent event 2 is NOT spent.** No further probe
text is written, no further embedding is paid for, and no further signal is
screened on the retrieval side.

**This supersedes the disposition recorded in `docs/DECISIONS.md`
**"2026-09-03 — Chunk 7.4: the OOD gate PASSES…"**, which stated that "classes
2–7 are **authorised** to proceed."** That authorisation is withdrawn here. The
gate's PASS **stands as run** and is not re-verdicted — what changes is the use
made of it, not the result. The 2026-09-03 entry carries a pointer annotation to
this entry; **its original text is unchanged**, per the void-convention stated in
this file's header.

**Why the authorisation is withdrawn, in one line:** the gate proved the families
can separate *something*, and chunk 7.4's own diagnostic then established that
what they separate is **corpus distance**, not answerability — so authoring
classes 2–7 would spend consent event 2 to re-measure a confound this pass has
already characterised. **INFERRED**, from the OBSERVED figures carried forward in
§2 below.

---

### 1. What is closed, and what is not

**Closed — OBSERVED scope:** the retrieval-side signal families **A** (distance-
profile shape), **B** (retrieved-set coherence), **C** (IDF-weighted coverage),
**E** (perturbation stability), as screening mechanisms for abstention on this
corpus. Family **G** (reranker score shape) was the control arm and is closed with
them.

**NOT closed, and untouched by this entry:** the Verifier line. The 2026-09-03
pre-registration's predicted-negatives on classes 3 and 4 localised abstention for
those classes to a component that reads retrieved text against the claim; that
prediction was never tested and is neither confirmed nor refuted here.
**NOT ESTABLISHED** — nothing in chunk 7.4 or 7.5 measured a Verifier.

---

### 2. FINDING carried forward

Three results are carried forward as the substance of the closed line. All are
restatements of figures already recorded; no new measurement is introduced by
this entry.

**(a) The retrieval-side signals separate OUT-OF-DOMAIN, not UNANSWERABLE.**
Top-1 corpus distance alone classifies the 12 class-1 OOD items against v3's 30
answerable at **AUROC 0.992 [0.967, 1.000]** — OBSERVED,
`chunk74-artifacts/inversion-diagnostic.md`. Any signal correlated with that
quantity inherits the separation without measuring answerability at all.

**(b) The sub-0.2 cluster's correlation with corpus distance COLLAPSES ON
CONDITIONING.** The nine signals that landed below AUROC 0.2 have mean
**|pooled r| = 0.590** with top-1 distance, against 0.303 for the other eight;
that correlation falls to **0.266 within answerable and 0.254 within OOD**.
`C1_idf_coverage` is the extreme case: **pooled −0.828, within-OOD +0.001**. All
OBSERVED, `chunk74-artifacts/inversion-diagnostic.md`. A pooled correlation that
vanishes on conditioning is a between-class shift, not a within-class
relationship.

**(c) In-domain near-miss unanswerability is NOT distance-separable.** The 6 v3
near-miss unanswerables have top-1 distance min **1.0201**, q1 **1.1043**, median
**1.3374**, q3 **1.4920**, max **1.5762**; per question u01 1.0201, u02 1.5762,
u03 1.2003, u04 1.4979, u05 1.4744, u06 1.0723. Their range straddles both other
classes — the minimum sits below the answerable median (1.0489) and three of the
six fall inside the OOD range [1.4131, 1.7829].

**OBSERVED (untracked cache).** These figures are recorded in
`DevBrain/rag-reliability/passes/chunk74p2-artifacts/phase2-discovery.md`, sha256
`77670891a1a8632dd581ef4712077431473d369a446f03ea6837415312a9d086`, §"DISTANCE
BAND — COMPLETED". **Their query vectors come from
`tests/fixtures/query_embeddings.json`, which is untracked and gitignored
(`.gitignore:23`) and is rewritten on any cache miss** — so they are not
reproducible from committed data, and the label is qualified accordingly. The same
qualification applies to the 30-answerable row. Only the 12 OOD rows trace to a
committed, sha256-pinned artifact.

**(c) is the load-bearing one.** (a) and (b) say the signals measure the wrong
thing; (c) says the right thing is not measurable by this route at all — the class
the Guardrail most needs to catch is the class that sits inside the answerable
band.

---

### 3. PROHIBITION — threshold fitting on this set is FORBIDDEN

Quoted verbatim from `docs/DECISIONS.md` **"2026-09-03 — PRE-REGISTRATION: the
abstain mechanism set"**:

> **SCOPE — MECHANISM TRIAGE ONLY. This artifact cannot be used to fit a
> threshold, and no later reader may repurpose it as one.**

and:

> It is not a validation set, not a calibration set, not a held-out set, and not a
> source of any number that gets written into Guardrail code.

and:

> **If a future pass wants a threshold, it must build a separate set for that
> purpose; reusing this one is threshold fitting on triage data and is
> prohibited.**

**That prohibition is in force and is not relaxed by this entry.**

**A cut sweep was nevertheless performed, and is recorded here so it cannot
resurface unlabelled.** On 2026-09-04 a sweep of candidate top-1 distance cuts
from 1.35 to 1.60 in steps of 0.01 was run and written to
`DevBrain/rag-reliability/passes/chunk75-artifacts/cut-selection.md`, sha256
`add02d222ce976569bd45f353ec6f2ef77189e22a40b3ae89e2b9bad1e287f4f`.

> **Its per-cut outcome table MUST NOT be used to select any threshold.** No cut
> was recommended in that document and none is adopted here. A reader who wants a
> threshold must build the separate set §3's prohibition requires.

**Two STRUCTURAL facts the sweep did establish — properties of the data, not
outcomes of a cut, and therefore admissible:**

1. **The overlap interval is [1.4131, 1.4932]**, width 0.0801, containing **1 of
   30 answerable and 3 of 12 OOD**: `a01` at **1.4932** lies *above* all three OOD
   members `ood08` 1.4131, `ood01` 1.4898, `ood10` 1.4906. OBSERVED.
2. **No cut achieves 30/30 answerable answered with 12/12 OOD abstained.** This
   is not a search result over the swept grid — it follows from fact 1, since a01
   sits above all three overlapping OOD items, and therefore holds at every cut
   value, inside the swept range and outside it. OBSERVED (fact 1) + INFERRED
   (the implication).

---

### 4. CONSTANT — `GUARDRAIL_FAR_FIELD_DISTANCE = 1.4932`

**UNFITTED PLACEHOLDER.** Recorded here as a decision; **no constant is committed
to `app/` or `scripts/` by this entry**, and no Guardrail code, stub or scaffold
was written.

**The selection rule, stated as the rule and not as the number:**

> **the maximum observed answerable top-1 distance; no observed answerable
> question is refused.**

**The rule was fixed in advance of consulting the sweep, and the value was NOT
selected by scanning sweep outcomes.** This is the whole basis on which the
constant is admissible under §3: a rule that names a single order statistic of
the negative class is not a fit, because it has no free parameter to fit — it
reads one value off the data and could have been written down before any sweep
existed. **Had the value been chosen by looking down the sweep's outcome columns
for a favourable trade, it would be threshold fitting on triage data and
prohibited.** It was not, and this sentence is the record of that.

**Status: UNFITTED, pending calibration on a held-out set that does not yet
exist.** **NOT ESTABLISHED** — no validation set, calibration set or held-out set
exists for this quantity; §3's prohibition forbids building one from the class-1
items.

**Known cost, recorded at adoption rather than discovered later — OBSERVED:**
**3 of the 12 class-1 OOD items fall below the cut and will NOT be refused:**

| id | top-1 distance |
|---|---|
| ood08 | 1.4131 |
| ood01 | 1.4898 |
| ood10 | 1.4906 |

That is a **25% miss rate on the easiest possible positive class**, and it is the
price of the rule's guarantee that no observed answerable question is refused. The
rule was chosen knowing this, not in spite of it.

---

### 5. GUARDRAIL CONTRACT — three states

| state | condition | meaning |
|---|---|---|
| **ANSWER** | — | the system answers |
| **ABSTAIN_OUT_OF_DOMAIN** | top-1 distance **≥** `GUARDRAIL_FAR_FIELD_DISTANCE` | the query is outside the corpus's subject; the system refuses |
| **ANSWER_UNVERIFIED** | top-1 distance **<** `GUARDRAIL_FAR_FIELD_DISTANCE`, groundedness **NOT ESTABLISHED** | the system answers and says so |

**`ANSWER_UNVERIFIED` is the honest carrier of the negative result.** Everything
inside the band is returned with its groundedness **NOT ESTABLISHED**, because
§2(c) established that in-domain unanswerability is not separable by this route.
**No in-band abstention is claimed.** The Guardrail refuses far-field queries and
makes no claim at all about the rest — which is what the measurements support, and
no more.

**The third state exists so the negative result is visible at the API boundary
rather than buried in this log.** A two-state contract would have to call
everything below the cut "answered", which would present an unmeasured property as
a verified one. **INFERRED** — this is a design consequence of §2(c), not itself a
measurement.

---

### 6. CARRIED DEFECTS — restated so closing the line does not lose them

Closing a line of investigation is where its open defects get dropped. These four
are restated in full so that does not happen.

1. **Disjunct C is UNIMPLEMENTED in the analysis code.**
   `chunk73-artifacts/phase3_analyse.py:62` computes `"dead": frac > 0.50` and
   nothing else; the count of positives inside the interval is computed and stored
   (`n_pos_in`, `:60`) but never enters the kill test. Chunk 7.4 applied disjunct
   C in a throwaway driver, not by running that script. **A reader who lifts
   `phase3_analyse.py` as-is would silently ship the pre-amendment rule.**
   OBSERVED — `chunk75-artifacts/port-inventory.md` §5.2, sha256
   `9a0204623ae59e032285f72d162651ea6b99f64cce5f69e7a1323b944a5cd704`.
2. **The kill-criterion conflict is UNRESOLVED.** The 2026-09-02 METHODOLOGY entry
   (`docs/DECISIONS.md:641`) writes the disjunct as **"more than half"**; the
   2026-09-03 pre-registration (`docs/DECISIONS.md:497-511`) tightens it to
   **≥ 6 of 12 / ≥ 3 of 6 — "exactly half now DIES"**. Both are in force in the
   log and they differ at exactly half. Chunk 7.4 ran under the tighter reading.
   **This conflict is NOT resolved here, and binds only if the measurement line is
   ever reopened** — resolving it now, with no pass pending, would be a rule
   written against known results. OBSERVED (both texts) + INFERRED (that they
   conflict).
3. **The phase 1 measurement path remains UNCOMMITTED.** The signal definitions
   and the W/C/AUROC criterion live only in the vault, at
   `chunk73-artifacts/phase2_signals.py` (sha256 `ef89985d…9bff`) and
   `phase3_analyse.py` (sha256 `aa54b5af…9b49`); neither is under version control,
   both fail on hard-coded `/tmp` paths, and no committed script runs the authoring
   probes, embeds a non-golden question set, or scores one against the frozen
   corpus. **Every chunk 7.3 and 7.4 figure depends on code the repo does not
   contain.** OBSERVED — `chunk75-artifacts/port-inventory.md` §§1–7.
4. **The vault/repo DECISIONS divergence is now FOUR entries, plus 21 vault-only.**
   Repo-only: 2026-08-24 (correction), 2026-08-25, and both 2026-09-03 entries.
   Vault-only: 21 Agentic-OS entries dated 2026-07-07 to 2026-07-16. OBSERVED —
   `chunk74p2-artifacts/phase2-discovery.md` §9.1. This entry makes it **five**
   repo-only. INFERRED: the vault-only 21 are scope difference rather than drift;
   nothing read states that policy.

---

### NOT ESTABLISHED

- **Any calibrated value for `GUARDRAIL_FAR_FIELD_DISTANCE`.** 1.4932 is an
  unfitted placeholder from a rule, not a calibrated threshold; the held-out set
  it needs does not exist and cannot be built from the class-1 items.
- **Groundedness of anything inside the band.** That is what `ANSWER_UNVERIFIED`
  records.
- **Whether the retrieval-side families would separate classes 2–7.** They are not
  authored; the prediction stands untested and is not resolved by closing the line.
- **Whether a Verifier separates classes 3 and 4.** Predicted in the 2026-09-03
  pre-registration, never measured.
- **Whether any of this generalises past this 260-chunk corpus and these 42
  questions.** No second corpus and no held-out set were measured.
- **Reproducibility of §2(c) and the 30-answerable figures from committed data.**
  Their vectors come from the untracked cache; see the qualification in §2.

---

**Evidence:**
`DevBrain/rag-reliability/passes/chunk74-artifacts/gate-report.md` sha256
`93ac4ecc14e2737e88d004e335e1021a7353bf79057a71d25bd9bb6022561056`;
`chunk74-artifacts/inversion-diagnostic.md` sha256
`923a0c3031ec77966055116847b45671e7a2b955126b7e917e2e1773eda4c586`;
`chunk74p2-artifacts/phase2-discovery.md` sha256
`77670891a1a8632dd581ef4712077431473d369a446f03ea6837415312a9d086`;
`chunk75-artifacts/port-inventory.md` sha256
`9a0204623ae59e032285f72d162651ea6b99f64cce5f69e7a1323b944a5cd704`;
`chunk75-artifacts/cut-selection.md` sha256
`add02d222ce976569bd45f353ec6f2ef77189e22a40b3ae89e2b9bad1e287f4f`.

Class-1 positives: the 12 items pinned at
`tests/fixtures/class1_query_vectors.{npy,json}`, `vectors_sha256`
`91aec7727cc271293cd4bd0c0fe6639d49864e45072206da3f83db040ec79489`; question texts
frozen at `class1-draft.json` sha256
`ce159eb4cf2d30fa18fe3105a4314873ae7d8d8358192b13a2c0bae4e64026eb`. Negatives:
golden set v3's 30 answerable, unchanged by this pass. Gate config as recorded in
the 2026-09-03 findings entry.

**No code was written, committed or scaffolded by this entry, and no constant was
added to `app/` or `scripts/`.** The 12-figure corpus-drift tripwire reproduced
exactly on 2026-09-04 before the sweep was run.

---

## 2026-09-03 — Chunk 7.4: the OOD gate PASSES — dynamic range EXISTS, and the signals that carry it are measuring DISTANCE FROM THE CORPUS

> **SUPERSEDED IN PART — see 2026-09-04 — "Chunk 7.5: the retrieval-side measurement line is CLOSED; classes 2–7 are NOT authored, and the Guardrail carries an UNFITTED far-field placeholder".** The gate's **PASS stands as run** and is not re-verdicted; what is withdrawn is this entry's disposition that "classes 2–7 are **authorised** to proceed" — they are NOT authored, and consent event 2 is NOT spent. Annotation only; the entry below is unchanged.

**Decision:** the pre-registered OOD-first gate **PASSES**. The retrieval-side
line of investigation is **NOT DEAD** on this corpus, and classes 2–7 are
authorised to proceed. Two signals in families A–E clear the full condition on
the 12 class-1 OOD items against v3's 30 answerable:

| signal | AUROC | 95% CI | overlap | positives inside | W | C | verdict |
|---|---|---|---|---|---|---|---|
| `A2_gap_1_mean` | **0.019** | [0.000, 0.064] | 11.8% | 4/12 | no | no | SURVIVES, CI excludes 0.5 |
| `C1_idf_coverage` | **0.025** | [0.000, 0.075] | 37.0% | 5/12 | no | no | SURVIVES, CI excludes 0.5 |

**The verdict stands as run.** It was computed under the criterion exactly as
pre-registered on 2026-09-03 — both disjuncts, the ≥6-of-12 boundary, the
untrimmed 30-item denominator — and **nothing in the diagnostic that followed
revised it, and nothing in it may be used to revise it later.** The diagnostic
pass promoted, demoted and re-verdicted no signal.

**What the gate establishes is exactly what it was built to establish and no
more: the discrimination task has dynamic range.** Chunk 7.3 could not separate
"these signals don't work" from "these six items were unseparable by
construction." That question is now answered: the families are not inert. They
can tell something apart on this corpus. They have not been shown to tell
*unanswerability* apart.

---

### THE PASS CONDITION HAS A DEFECT. IT IS RECORDED HERE, NOT REPAIRED.

**The condition says nothing about DIRECTION, and both passing signals separate
in the direction OPPOSITE to their pre-registration.**

The gate requires "an AUROC confidence interval excluding 0.5." A CI excluding
0.5 from **below** satisfies that wording exactly as well as one excluding it
from above. Both survivors excluded it from below: A2 at 0.019 and C1 at 0.025
are near-perfect inversions. The pre-registration predicted family A as **SEP —
flatter curve, no elbow, larger top-1 distance** and family C as **SEP —
genuinely disjoint vocabulary, real corpus-absence**, both in the ordinary
direction. Neither landed in the direction predicted. The gate passed anyway,
because the condition it was written to test does not read direction.

**This is a defect in the pre-registered rule's DESIGN, not in its execution.**
The rule was applied faithfully to every signal — nothing was mis-run,
mis-computed or mis-reported. What failed is **what the rule measured**. "The CI
excludes 0.5" measures *that* the classes separate; it is silent on *which way*,
and a family-level directional prediction that the same document states in the
same breath is then never checked against the result. Those are different
quantities and on this data they came apart.

**The condition is NOT AMENDED here, and the reason is the whole point.**
Tightening a gate condition after seeing which signals it passed is precisely
the post-hoc move `chunk73-REPORT.md` §13.2 forbids, and the 2026-09-02
METHODOLOGY entry states the exemption does not exist: **a rule tightened after
seeing which signals it spared is still a rule fitted to data, and being stricter
doesn't exempt it.** The same discipline that made 7.3's negative result
trustworthy applies to 7.4's positive one. The gate stands as written and as run.

**Binding on every future pass: a gate condition that carries a directional
prediction must test direction in advance.** The remedy is a better
pre-registered rule next time, not a rewritten one this time. Pre-registration
discipline is not weakened by this entry; it is what forces the defect to be
recorded rather than quietly fixed.

---

### THE INVERSION IS CORPUS-DISTANCE, MEASURED

Nine of 17 signals landed below AUROC 0.2. The cause is measured, not inferred:

- **Top-1 distance ALONE classifies 12-vs-30 at AUROC 0.992 [0.967, 1.000].**
  Any signal correlated with it inherits that separation for free.
- The nine sub-0.2 signals have **mean |pooled r| = 0.590** with top-1 distance,
  against 0.303 for the other eight.
- That correlation **collapses on conditioning: 0.590 pooled → 0.266 within
  answerable, 0.254 within OOD.**
- `C1_idf_coverage` is the extreme case: **pooled r = −0.828, within-OOD
  r = +0.001.**

**A pooled correlation that vanishes when you condition on class is a
between-class shift, not a within-class relationship.** These signals are
measuring **distance from the corpus**, not answerability. That is what carried
the gate.

---

### THE MECHANISM IS SPLIT. THE TWO HALVES ARE NOT THE SAME FINDING AND ARE NOT TO BE COLLAPSED.

**Half one — "flat profiles read as coherence." Holds for family A and for C1.**
OOD retrieved profiles are genuinely flatter: std of top-30 **0.031 vs 0.066**,
fall-off **0.082 vs 0.191** (class medians, OOD vs answerable). A flat far-field
profile has no elbow and no gap, so the shape signals read it as the absence of
the thing they were built to detect. C1 needs no mediation argument at all: an
OOD query's high-IDF lexemes are by construction absent from the corpus, so
coverage sits near floor (median **0.145 vs 0.581**).

**Half two — B and E do NOT work this way, and saying they do would be wrong.**
The OOD retrieved sets are **LESS** coherent (B1 **0.681 vs 0.723**; B2 0.418 vs
0.525) and **LESS** stable under perturbation (E2 **0.548 vs 0.667**; E3 0.837 vs
0.916) — the opposite of the answerable class on both counts. Flatness did not
mimic coherence here. These invert because **the pre-registered direction was
wrong**: the prediction was that abstain cases score low on coherence and
stability, they do score low, and the answerable class scores lower still is not
what happened — what happened is that the family's founding premise put the
classes the other way round. A flat, far neighbourhood reshuffles easily and its
members are mutually unrelated.

**Two mechanisms, recorded separately.** One is a measurement artifact of
distance; the other is a wrong directional premise. Collapsing them into "the
signals inverted" would lose the distinction that matters for classes 2–7.

---

### C1's SIGN REVERSAL — WHAT IT DOES AND DOES NOT ESTABLISH

7.3 measured `C1_idf_coverage` at **0.567**, the ordinary direction, on the six
v3 near-misses. 7.4 measures it at **0.025** on the 12 OOD items, against the
same 30 negatives, under the same config and the same code. **This is a sign
reversal, not a sharpening of a weak result.** The two numbers sit on opposite
sides of chance.

**What it establishes:** the family-C behaviour recorded in 7.3 is **not a fixed
property of the signal on this corpus.** It changes sign with the positive
class. A result that flips when only the positive class is swapped is a
measurement of the positive class, which is the proposition the whole
`abstain_mechanisms` artifact was pre-registered to test, and on that narrow
point the answer is yes.

**What it does NOT establish — and this is where the pre-registration's own test
must be read precisely.** The 2026-09-02 paraphrase-distance refutation is a
finding about **C2**, `C2_corpus_absence`, not about C1; the pre-registration's
"direct test of whether the 7.3 refutation is recipe-bound" is stated at family
level with its rationale written in terms of C2. C2's own 7.4 result is
therefore the one that bears on it, and it is this: **C2 fired on 12 of 12 OOD
items** (against 21 of 30 answerable), AUROC 0.650 [0.567, 0.717] — it *did*
fire honestly on a vocabulary-disjoint positive class, exactly as the
pre-registration predicted it should. **And it is still DEAD**, killed by
disjunct C: as a two-valued indicator its overlap interval is the single point
6.2577, which contains all 12 positives. **The pre-registration flagged this
ceiling in advance** — "C2 is a two-valued indicator (`idf(0)` is a constant), so
its resolution is categorical regardless of outcome."

So: whether the paraphrase-distance refutation generalises past the v3 recipe is
**NOT ESTABLISHED**. C2 fired where the refutation said it structurally could
not, which is evidence the refutation is recipe-bound; but it fired
categorically, on a class where it cannot fail to fire, and was killed by the
criterion anyway. C1's reversal is a different signal answering a different
question. **Neither number settles it, and it is left open rather than resolved
in the direction either one points.**

---

### THE GATE CANNOT SEPARATE UNANSWERABILITY FROM FAR-FIELD. THIS BOUNDS WHAT CLASS 1 ESTABLISHED.

**The two classes barely touch.** The 10 hardest answerable questions — the ones
with the largest top-1 distance — top out at **1.4932**. The OOD class *starts*
at **1.4131**. Answerable spans [0.7189, 1.4932]; OOD spans [1.4131, 1.7829].
**Every result in this pass is confounded between "the question is unanswerable"
and "the query is far from the corpus," and no comparison available in this data
separates them.**

**The control arm reached 0.994 and must be recorded plainly.** `G3_softmax_entropy`
— reranker softmax entropy, 7.3's labelled control — scored **AUROC 0.994
[0.975, 1.000]** on 12-vs-30, the largest figure anywhere in the pass. The
pre-registration predicted only **"SEP, weak"** for family G on class 1, and
stated the reason it was writing that down: "so a class-1-only success cannot
later be presented as a surprise." It is not presented as a surprise. It is
recorded as far larger than predicted. G3's class ranges do overlap —
[3.2262, 3.3850], holding 2 of 12 OOD and 1 of 30 answerable — so the separation
is near-total, not clean.

Restricted to the 10 hardest answerable as negatives, G3 holds at **0.983
[0.925, 1.000]**. That is **evidence against G3 being a pure far-field
detector**. It is **NOT clean evidence that G3 detects unanswerability**: even
those 10 sit below most of the OOD set — their maximum, 1.4932, is barely above
the OOD minimum, 1.4131 — so the restriction cannot break the confound, only
strain it. Recorded as a diagnostic, not a verdict, and not promoted: G is the
control arm and takes no part in the gate.

---

### WHAT THIS MEANS FOR CLASSES 2–7

**Class 1 established dynamic range and NOTHING MORE.** It is the easiest
possible positive class by design, and it turned out to be easy for a reason the
gate does not care about: the queries are far from the corpus. The gate's job
was to rule out inertness, and it did.

**Classes 2–7 are in-domain.** Their queries share the corpus's subject and
vocabulary, so they will sit **inside the answerable distance band** — which is
exactly where the corpus-distance confound disappears and the real test of these
signal families lives. Class 1 could not run that test. Classes 2–7 can.

**No per-class n is set here. No item is authored here. Nothing beyond the
authorisation is decided here.** The gate's PASS authorises proceeding; what
proceeding looks like is not settled by this entry.

---

**Evidence:**
`DevBrain/rag-reliability/passes/chunk74-artifacts/gate-report.md` sha256
`93ac4ecc14e2737e88d004e335e1021a7353bf79057a71d25bd9bb6022561056`;
`DevBrain/rag-reliability/passes/chunk74-artifacts/inversion-diagnostic.md` sha256
`923a0c3031ec77966055116847b45671e7a2b955126b7e917e2e1773eda4c586`.

Gate config, identical to 7.3's primary arm: arm `vector`, `candidate_k=30`,
`rerank_n=40`, `k_curve=40`, sigma 0.20, 20 draws, `seed_base=20260902`, local
ONNX reranker; AUROC by mid-rank Mann-Whitney with 1000 bootstrap resamples,
seed 20260902. Signal definitions spliced byte-identical from
`chunk73-artifacts/phase2_signals.py`; `auroc`/`boot`/`overlap` spliced
byte-identical from `phase3_analyse.py`.

Positive class: the 12 class-1 OOD items, vectors pinned at
`tests/fixtures/class1_query_vectors.{npy,json}`, `vectors_sha256`
`91aec7727cc271293cd4bd0c0fe6639d49864e45072206da3f83db040ec79489`, verified
before every use; question texts frozen at class1-draft.json sha256
`ce159eb4cf2d30fa18fe3105a4314873ae7d8d8358192b13a2c0bae4e64026eb`, each
verified by a zero-row subject-noun `ILIKE` probe (12 of 12 zero rows).
Negative class: v3's 30 answerable; v3's 6 unanswerables excluded as
pre-registered.

**The 12-figure corpus-drift tripwire reproduced exactly** before any comparison
was computed — lexical `0.233 0.400 0.567 0.142`, vector `0.833 0.900 1.000
0.700`, hybrid `0.667 0.867 0.967 0.445`, `NoOpReranker`, golden set v3 — so the
corpus and index had not moved under the benchmark. Golden set v3 unchanged by
this pass.

---

## 2026-09-03 — PRE-REGISTRATION: the abstain mechanism set — TRIAGE ONLY, OOD-first, and classes 3 and 4 stay SPLIT

**Decision:** an additive artifact, the **abstain mechanism set**
(`abstain_mechanisms`), is pre-registered here — its purpose, its seven
mechanism classes, its per-family directional predictions, its gate order, and
the constraints binding it — **before any probe question is authored and before
any vector is embedded.** No probe text exists yet. No per-class n exists yet
for classes 2–7.

**SCOPE — MECHANISM TRIAGE ONLY. This artifact cannot be used to fit a
threshold, and no later reader may repurpose it as one.** Its entire job is to
answer three coarse questions per mechanism class: *is the class separable from
answerable at all*, *in which direction*, and *roughly how large is the effect*.
It is not a validation set, not a calibration set, not a held-out set, and not a
source of any number that gets written into Guardrail code. A signal that
triages well here is still "survives screening, UNVALIDATED" and still owes the
same evidence every 7.3 survivor owes. **If a future pass wants a threshold, it
must build a separate set for that purpose; reusing this one is threshold
fitting on triage data and is prohibited.**

**The root finding this answers: golden set v3's six unanswerables are six
instances of one recipe, not a sample of unanswerability.** All six were
authored to the same specification — *share vocabulary with the corpus* — and
that single authoring choice is what two of chunk 7.3's findings turn on. See
`docs/DECISIONS.md` **"2026-09-02 — Chunk 7.3: IDF-weighted rare-term absence
measures PARAPHRASE DISTANCE, not unanswerability"**, which records that a
rare-term-absence detector *structurally cannot fire* on items engineered for
vocabulary overlap; and **"2026-09-02 — Chunk 7.3: perturbation stability is
INVERTED — unanswerable queries sit in MORE stable neighbourhoods"**, which
records that a near-miss is by construction a query landing in dense,
well-supported corpus territory. Both refutations are *scoped to what a near-miss
is*. Neither tells us anything about a question that is unanswerable for a
different reason. **Six items of one recipe is a measurement of that recipe, not
of the concept.** Chunk 7.3's negatives stand as run and are not reopened by this
entry; what is reopened is only the question of whether they generalise past the
recipe.

---

### The seven mechanism classes

Each class is defined by **why the answer is unavailable**, not by surface form.
The `differs from` column is the discriminating question a later author must be
able to answer for every item.

| # | Class | Definition | Why the answer is unavailable | Differs from its nearest neighbour |
|---|---|---|---|---|
| **1** | **out-of-domain** | The question's subject matter is outside the corpus's subject entirely. | The corpus never discusses this domain; no chunk is even topically relevant. | vs **5**: 1 is a different *subject*; 5 is the right subject at a depth or facet the document does not cover. |
| **2** | **in-domain entity-absent** (= the v3 recipe) | Right subject, right vocabulary, but the specific entity/fact asked for is not in the corpus. | The corpus covers the neighbourhood densely and this particular item is simply not in it. | vs **1**: 2's vocabulary is shared with the corpus by construction; retrieval returns confident, topically-correct, wrong chunks. |
| **3** | **multi-chunk synthesis unsupported** | The answer would require combining facts across chunks in a way the corpus does not license (a comparison, total, or derivation whose inputs are not all present or not commensurable). | Every *individual* retrieved chunk is relevant and correct; the **aggregation** is what has no support. | vs **4**: 3 fails at the **aggregation level** — the premises are true, the combination is unsupported. |
| **4** | **false premise / corpus-contradicting** | The question presupposes something the corpus explicitly contradicts. | The **premise** is false against the corpus; there is no answer because the question should not be answered as asked. | vs **3**: 4 fails at the **premise level** — the inputs themselves are wrong, not the way they are combined. |
| **5** | **scope-out-of-bounds** | Right subject, but the question asks for a facet, depth, or artifact class the document does not cover (e.g. implementation detail in a conceptual overview). | The corpus is topically adjacent but structurally silent on this kind of content. | vs **1**: retrieval returns genuinely on-topic chunks, unlike 1. vs **2**: 5 is missing a *kind* of content, not a specific entity. |
| **6** | **granularity mismatch** | The corpus holds the information at a different level of aggregation than asked (per-item asked, aggregate given, or vice versa). | The right chunk is retrieved and is *about* the thing asked, at the wrong resolution. | vs **5**: 6's content class **is** covered; only the resolution is wrong. vs **3**: 6 needs no cross-chunk combination. |
| **7** | **ambiguous referent** | The question is underspecified and could refer to several distinct corpus entities, with different answers. | There is no *unique* answer; answering requires a disambiguation the asker did not supply. | vs **2**: the referent exists — several times over. The failure is uniqueness, not absence. |

**Classes 3 and 4 stay SPLIT. Collapsing them into one "the retrieved chunks
don't support the answer" class is PROHIBITED.** They fail at different levels —
3 at aggregation, 4 at premise — and an abstention mechanism that catches one
need not catch the other: a Verifier checking "is this claim entailed by the
retrieved text" behaves differently from one checking "does the retrieved text
contradict the question's presupposition." **This is the same discipline applied
in chunk 7.1's confident-misjudgement vs near-tie ordering-flip split, kept split
in `chunk73-REPORT.md` §10.1 even though that pass could not establish it:**
"*Rank movement alone does not separate a25 (1→7) from a03 (1→7)… no such check
was run.*" The split was preserved there **because** the distinguishing evidence
was missing, not despite it. Same here: 3 and 4 stay split until something
measures them apart, and "they look similar on the signals we happened to have"
is a reason to keep them split, never a reason to merge them.

---

### AUTHORING TEST — the class 1 / class 5 boundary, made CHECKABLE

The 1-vs-5 boundary is a **continuum** — "a different subject" and "the right
subject at an uncovered facet" shade into each other — and nothing in the table
above makes the line checkable by anyone but the author's judgement. It is made
checkable here, reusing **golden set v3's `absence_evidence` pattern**: an
absence claim is not asserted, it is demonstrated with a corpus query and its row
count.

- **Class 1 qualifies only if a subject-noun `ILIKE` probe against the corpus
  returns ZERO rows.**
- **Class 5 qualifies only if the subject-noun probe returns NON-ZERO rows AND a
  qualifier probe returns ZERO rows.**

Every authored item carries **its SQL and its row counts, in the same shape as
v3's `absence_evidence`**, in the artifact.

**An item that fails its class's test is DISCARDED AND REPLACED, never
reclassified into the other class.** Reclassification would let the partition be
drawn to fit whatever text happened to get written — the class boundary would
then be a description of the items rather than a constraint on them, which is the
exact defect this artifact exists to escape.

**Class 1's test is BINDING NOW.** Class 5's test binds only if the gate passes
and classes 2–7 are authored.

---

### DIRECTIONAL PREDICTIONS — pre-registered, per SIGNAL FAMILY

Predictions are written **per family, never per individual signal.** Family names
are chunk 7.3's own, taken verbatim from the section comments of the code that
pass ran (`chunk73-artifacts/phase2_signals.py`): **A** distance-profile shape
(:120), **B** retrieved-set coherence over `included` (:138), **C** IDF-weighted
coverage (:159, the brief's "query-term coverage"), **D** answer-type presence
(:176), **E** perturbation stability (:186), **G** reranker score shape (:202,
7.3's labelled control arm). No family name is invented.

Legend — **SEP** = separates, with the stated direction; **NO** = does not
separate; **INV** = separates in the direction *opposite* to the family's
founding premise.

| Family | 1 out-of-domain | 2 entity-absent | 3 synthesis | 4 false premise | 5 scope | 6 granularity | 7 ambiguous |
|---|---|---|---|---|---|---|---|
| **A** distance-profile shape | **SEP** — flatter curve, no elbow, larger top-1 distance | **NO** (replication of 7.3) | **NO** (predicted-negative) | **NO** (predicted-negative) | **SEP, weak** — intermediate between 1 and 2 | **NO** | **NO** |
| **B** retrieved-set coherence | **SEP** — incoherent grab-bag: lower pairwise cosine, more index runs | **NO** | **NO** (predicted-negative) | **INV, weak** — *more* coherent than answerable | **SEP, weak** — lower coherence | **NO** | **SEP** — set spans several distinct entities: lower coherence, more runs |
| **C** IDF-weighted coverage | **SEP** — genuinely disjoint vocabulary, real corpus-absence | **INV** (7.3 refutation: fires more on answerable) | **NO** (predicted-negative) | **NO** (predicted-negative) | **SEP, weak** | **SEP, very weak** — the granular term may be absent | **NO** |
| **D** answer-type presence | **NO** — detector was near-constant in 7.3 and is expected to stay uninformative | **NO** | **NO** | **NO** | **NO** | **NO** | **NO** |
| **E** perturbation stability | **SEP** — sparse, arbitrary neighbourhood reshuffles under jitter (the family's *original* direction) | **INV** (7.3 refutation: near-misses are *more* stable) | **NO** (predicted-negative) | **NO** (predicted-negative) | **SEP, weak** | **NO** | **SEP** — query sits on a cluster boundary; jitter flips which cluster wins |
| **G** reranker score shape (control) | **SEP, weak** — higher softmax entropy, smaller margin | **NO** (7.3: all three DEAD) | **NO** | **NO** | **NO** | **NO** | **NO** |

**Family-level rationale.**

- **A, B, C, E separate on class 1 because class 1 is the only class where the
  retrieval geometry is genuinely degraded.** Every other class retrieves
  something topically right; class 1 does not.
- **C's class-1 prediction is the direct test of whether the 7.3 refutation is
  recipe-bound.** C2 was refuted *on v3's recipe*. Class 1 is the case where
  corpus-absence should fire honestly. If it does not fire even here, the
  refutation generalises past the recipe. Ceiling noted in advance: C2 is a
  two-valued indicator (`idf(0)` is a constant), so its resolution is
  categorical regardless of outcome.
- **E's class-1 and class-7 predictions are in the family's ORIGINAL direction,
  which 7.3 refuted on class 2.** This is not a re-litigation of that refutation.
  7.3's finding was that a *near-miss* sits in dense territory; a class-1 query
  and a class-7 boundary query do not. If E separates class 1 in the original
  direction, both results are true and the family's scope is what changes.
- **D is predicted uninformative everywhere.** It was NOT ESTABLISHED in 7.3 for
  detector-near-constancy, and nothing in this design fixes the detector. It is
  carried so its null is recorded, not because it is expected to work.
- **G is carried as the control arm, exactly as in 7.3, with the same
  pre-registered expectation that it mostly fails.** Its one weak SEP on class 1
  is stated so a class-1-only success cannot later be presented as a surprise.

**PREDICTED-NEGATIVES on classes 3 and 4 are predictions, with a rationale, and
a landed predicted-negative is a POSITIVE FINDING.**

The rationale: in classes 3 and 4 **retrieval does its job correctly.** The
right chunks come back, ranked confidently, with a normal distance profile and
normal coherence. Class 3's failure is that the *combination* of correct chunks
is unlicensed; class 4's is that the *question's premise* is contradicted by the
correct chunks. Neither failure is visible in the geometry of what was retrieved,
because neither is a retrieval failure. Retrieval-side families are therefore
predicted **not to separate**, and that prediction is made now, in advance, so it
cannot be reinterpreted afterwards.

**If those predicted-negatives land, that is a positive finding about where the
signal must live, not another null.** It localises abstention for classes 3 and 4
to the **Verifier** — a component that reads the retrieved text against the
claim — and rules out the retrieval side for them by direct measurement rather
than by assumption. A null that was predicted, with a mechanism, and that lands,
is evidence. **This must not be counted alongside 7.3's nulls in any later "the
signals didn't work" summary**; it is a different kind of result and is to be
reported as such.

---

### THE OOD-FIRST GATE

**Class 1 is authored and measured FIRST, alone, as its own gate. Classes 2–7 are
not authored until it returns.**

**The gate's job is to prove the discrimination task has dynamic range** — that
these signal families can tell *anything* apart on this corpus. Chunk 7.3 could
not distinguish "these signals don't work" from "these six items were
unseparable by construction." Class 1 is the easiest possible positive class. If
the retrieval-side families cannot separate a question about an entirely
different subject from an answerable one, the families are not weak — they are
inert, and no amount of better-designed unanswerables will change that.

**Comparison, fixed now:**

- **Positive class: the 12 class-1 OOD items ONLY, n = 12.**
- **Negative class: golden set v3's 30 answerable questions.**
- **v3's six unanswerables are EXCLUDED from this comparison.** Pooling them into
  the positive class would reintroduce the single-recipe confound this entire
  artifact exists to escape — a 18-item positive class that is two-thirds one
  recipe measures the recipe again, with extra steps. They are excluded, and
  their exclusion is pre-registered here so it cannot later look like a choice
  made after seeing a result.

**Gate outcome, pre-registered. PASS and FAIL are the only two outcomes; there
is no third.**

> **FAIL — if no retrieval-side family (A, B, C, E) separates the 12 OOD items
> from the 30 answerable under the full kill criterion below, the retrieval-side
> family is DEAD for abstention on this corpus, and classes 2–7 are NOT
> AUTHORED.**

> **PASS — the gate PASSES if at least one signal in families A, B, C or E both
> (i) survives disjuncts W and C, and (ii) has an AUROC confidence interval
> excluding 0.5, on the 12 OOD vs 30 answerable comparison.**

The FAIL outcome ends the retrieval-side line of investigation and redirects the
work to the Verifier. It is a real, accepted, pre-registered possibility, not a
formality.

**Why clause (ii) is required, and why survival alone is not a pass.** The kill
criterion returns only **DEAD / not-DEAD**, and not-DEAD is merely "survives
screening, UNVALIDATED" — which chunk 7.3 showed is not separation: A5, B4 and A3
all survived while their overlap intervals held 6 of 6, 5 of 6 and 6 of 6 of the
abstain class (`chunk73-REPORT.md` §13.1). Without clause (ii) this gate could
pass on exactly that thin survival, leaving the **dynamic-range** question
unanswered — and settling that question is the single thing the gate exists for.

**The criterion the gate runs under** — both disjuncts quoted verbatim, applied
as one rule. A signal is **DEAD** if **either** holds.

> **(W — width.** `chunk73-REPORT.md` §9, lines 466–469, **the criterion as run in 7.3**.**)**
>
> > A signal is **DEAD** if the overlap interval covers more than 50% of the ANSWER
> > class's observed range on the **ABSTAIN_UNANSWERABLE** comparison. The
> > **ABSTAIN_BUDGET** comparison is **NOT ESTABLISHED** in this configuration and
> > contributes **nothing either way**.
>
> **(C — concentration.** `docs/DECISIONS.md` lines 21–22, = `chunk73-REPORT.md` §13.2, pinned for the next screening pass — **this is that pass.**)
>
> > **A signal is also DEAD if its overlap interval contains more than half the
> > abstain class, regardless of interval width.**
>
> with, verbatim from `chunk73-REPORT.md` §7:
>
> > **Overlap interval** = the value range where both classes coexist, i.e.
> > `[max(min_abstain, min_answer), min(max_abstain, max_answer)]`

Survival requires passing **both**. Disjunct C can only kill, never revive.

**On naming — AMB-4 is resolved by these labels.** The 7.3 report uses one word
for two different changes: dropping the `ABSTAIN_BUDGET` comparison (§9) and
adding the concentration disjunct (§13.2). This entry therefore refers to the two
disjuncts as **W** and **C** and **never uses the bare word "amended"** for
either. **The ancestor two-comparison wording is NOT NEEDED and is not an open
question:** this pre-registration attaches to the rule **as actually executed in
7.3**, not to any ancestor of it, and the dropped `ABSTAIN_BUDGET` comparison is
**empty by construction** — it contributes nothing either way and is not
re-litigable.

**Three points the criterion's wording does not settle are fixed now, and the
fixes are binding** — they are not left for the analyst to settle mid-pass.
Item 1 tightens the rule; items 2 and 3 are **new pre-registration decisions**,
not readings of the source text.

1. **"more than half" at n=12 — HALF OR MORE KILLS.** The disjunct-C boundary is
   **≥ 6 of 12** inside the overlap interval, not ≥ 7 of 12. **Exactly half now
   DIES.** The n=6 equivalent becomes **≥ 3 of 6**.

   **Why this tightening is pre-registration and not post-hoc fitting: it is made
   BEFORE any probe text is authored, BEFORE any embedding, and BEFORE any
   measurement.** §13.2 forbids tightening a rule *after seeing which signals it
   spared*. **Nothing has been spared here, because nothing has been measured** —
   there is no result for this rule to have been fitted to. That timing is the
   justification, and it is the whole justification.

   Substantively: **an exact-half band is a coin flip, and sparing a signal there
   is not evidence of separation.** A rule whose job is to kill signals that fail
   to separate should not hand survival to a signal whose overlap interval holds
   half its positive class.
2. **NEW PRE-REGISTRATION DECISION — not a reading of §9 or §13: "the abstain
   class" when the positive class is not `ABSTAIN_UNANSWERABLE`.** Disjunct W
   names that v3 label explicitly; the 12 OOD items carry no v3 label. The source
   text does not settle this, and the decision made here is: **"the abstain class"
   means whichever positive class the comparison is defined over — here, the 12
   OOD items.** W's reference to `ABSTAIN_UNANSWERABLE` is taken as identifying
   7.3's positive class, not as restricting the rule to that label.
3. **NEW PRE-REGISTRATION DECISION — not a reading of §9 or §13: W's denominator,
   "the ANSWER class's observed range".** Fixed in advance as **all 30 of v3's
   answerable questions**, and **not trimmed after seeing results under any
   justification.** `chunk73-REPORT.md` §7.2 is the reason: dropping 8 ceiling
   items moved E1's overlap from 92.3% to 100.0%. The denominator is a lever and
   it is nailed down now.

**Class 1 n = 12, with its rationale.** At n=6, disjunct C resolves at 1/6
granularity with the kill boundary at 3/6 — **a single item moving in or out of
the overlap interval decides the verdict.** Every one of 7.3's survivors sat at
5/6 or 6/6. n=12 halves the granularity to 1/12, moves the boundary to 6/12, and
removes single-item decisiveness.
**INFERRED, and explicitly NOT a power calculation** — no power analysis was run
and none is claimed. It is arithmetic about how few items it currently takes to
flip a verdict.

**Per-class n for classes 2–7 is NOT set by this entry** and must not be inferred
from 12. It is set only if the gate passes, in a follow-up entry.

---

### HARD CONSTRAINTS — binding

1. **Authoring proceeds WITHOUT consulting chunk 7.3's per-item signal values.**
   `chunk73-signals.json` and `phase3_stats.json` are not read while probe
   questions are being written. Authoring a probe against known per-item values
   is fitting the set to the signals.
2. **Golden set v3 stays byte-identical.** `tests/fixtures/golden_set_v3.json`,
   sha256 `2ca9c82b263e73619c8896c6739739943a2b6affb351e8187f1a340e01ff2529`
   (OBSERVED, verified this pass). The abstain mechanism set is a **separate,
   additive artifact** and is **read by NO gate input** — no benchmark, no DoD
   check, no chunk gate consumes it.
3. **No re-embedding of the corpus.** 260 chunks, voyage-4-lite, frozen.
   `document_id doc_01M0D39WZDYY7STA3PHWT5R4C7` unchanged (OBSERVED at
   `golden_set_v3.json` → `corpus.document_id`, `corpus.chunks` = 260). Only
   *query* vectors are ever embedded by this work.
4. **Mechanism classes are retro-tagged onto v3's six unanswerables in the NEW
   artifact's metadata, keyed by v3 `id`, and v3 is never written.** (All six are
   expected to tag as class 2 — that is the root finding, recorded as a tag, not
   asserted as a new result.) **Evidence that `id` is a safe key** (OBSERVED,
   parsed from `entries[].id` this pass): **unique 36 of 36**; and
   **non-positional** — `ids[5]` is `a07`, not `a06`, and `a06`, `a08`, `a12`,
   `a16`, `a26` are all **absent** from the set. Positional indexing into v3
   would therefore silently mis-key; `id` would not.
5. **Naming: "abstain mechanism set" / `abstain_mechanisms`. "Probe set" is
   PROHIBITED as a name for it.** That term is already bound: `docs/DECISIONS.md`
   line 1453, inside the 2026-08-20 "golden set v3 APPROVED" entry, uses "probe
   sets" for the **absence-SQL queries** run against the corpus when authoring a
   v3 unanswerable — "*Five of six probe sets initially returned non-zero rows*".
   Reusing the word would collide two different things inside the same document,
   one of them nested inside the other's provenance.

---

### EMBEDDING PLAN — recorded before it runs

**Batching is confirmed supported** (OBSERVED): `app/services/embeddings.py:23`
`VOYAGE_MAX_TEXTS_PER_REQUEST = 1000` and `:26`
`VOYAGE_MAX_TOKENS_PER_REQUEST = 100_000` — both ceilings are far above anything
this artifact needs. The call is list-in/list-out with **order preserved**:
`:141` `"input": texts,` and `:163`
`return [item["embedding"] for item in payload["data"]]`. Every batch here is a
single request.

**TWO consent events, not one.**

1. **Event 1 — the 12 OOD queries only.** Nothing else is embedded.
2. **Event 2 — classes 2–7 — is requested ONLY IF THE GATE PASSES.** If the gate
   fails, event 2 never happens and no further money is spent. Bundling both into
   one approval would pre-commit spend to a branch the gate is supposed to be
   able to close.

**Gate path** (OBSERVED): `load_query_vectors(..., allow_network=True)`,
`app/services/evaluation.py:278-284` — the `if missing and not allow_network:`
branch whose own error text says it "*is the only place in this module that can
spend money*". `allow_network=True` is passed at exactly the two consent events
above and nowhere else.

**Pinning — the obvious approach does not work, and the reason is recorded.**
`tests/fixtures/query_embeddings.json` is **gitignored** (OBSERVED:
`git check-ignore -v` → `.gitignore:23`) and **untracked** (`git status
--porcelain` returns nothing for it), and it is **fully rewritten on every
miss** — `app/services/evaluation.py:291-292` does
`cache_path.write_text(json.dumps(cache))` over the whole file, with no
append-only path. **Hashing that file pins nothing**: its digest changes whenever
any unrelated query is embedded, and it is not in the repository to be pinned
against in the first place.

**Instead: extract the probe-only vectors into a separate tracked artifact and
pin its sha256, following the corpus pattern.** The corpus does exactly this —
`app/corpus/corpus_vectors.json:5` carries `"vectors_sha256": "dd4c…7100"`, and
`app/services/vector_store.py:208-212` recomputes the digest on load and raises
`CorpusUnavailableError` on mismatch. **Fatal, not a warning.** The abstain
mechanism set's vectors get the same treatment: tracked file, digest in the
manifest, hard failure on drift.

---

**Nothing in this entry is a result.** No probe question text was authored. No
vector was embedded. No signal was computed. No threshold is proposed, and none
may be derived from this artifact when it exists. This is a pre-registration and
its only claim is about what will be measured, in what order, and what will
count as failure.

**Evidence:** `DevBrain/rag-reliability/passes/chunk73-artifacts/chunk73-REPORT.md`
§9 (criterion as run, lines 462–475), §7 (overlap-interval definition, 335–338),
§7.2 (ANSWER-range sensitivity), §10.1 (the 7.1 split precedent, 529–547), §13
(criterion defect, concentration disjunct, observation, 652–707); `phase2_signals.py` lines
120/138/159/176/186/202 (family names, verbatim); `docs/DECISIONS.md` lines 21–22
(concentration disjunct), 80 and 138 (the two 7.3 refutation entries), 1453
("probe set" already bound); `tests/fixtures/golden_set_v3.json` sha256
`2ca9c82b263e73619c8896c6739739943a2b6affb351e8187f1a340e01ff2529`, 36/36 unique
non-positional ids, `corpus.document_id doc_01M0D39WZDYY7STA3PHWT5R4C7`;
`app/services/embeddings.py:23,26,141,163`;
`app/services/evaluation.py:278-284,291-292`; `app/corpus/corpus_vectors.json:5`;
`app/services/vector_store.py:208-212`; `.gitignore:23`. Full read log and
NOT-ESTABLISHED list:
`DevBrain/rag-reliability/passes/chunk74-artifacts/phase1-notes.md` sha256
`778091144386827ebeddfce3b27f7a1f605126e97baf86c01f01b792a0884281`.

---

## 2026-09-02 — METHODOLOGY: a screening kill criterion must measure CONCENTRATION, not just interval WIDTH

**Decision:** the overlap-interval kill criterion used to screen candidate signals is amended, for
**every future screening pass**, with an additional disjunct:

> **A signal is also DEAD if its overlap interval contains more than half the abstain class,
> regardless of interval width.**

It is an additional disjunct, never a replacement: it can only ever **kill** a signal, never revive
one, so it strictly tightens the rule and cannot be used to rescue anything.

**Scope: this is a methodology correction, not a chunk 7.3 finding.** It governs how any future pass
screens any candidate signal against any positive class. Chunk 7.3 is where the defect surfaced; it
is not where the defect lives.

**The defect is in the PRE-REGISTERED RULE'S DESIGN, not in its execution.** The rule was applied
faithfully, as written, to every signal in chunk 7.3 — nothing was mis-run, mis-computed or
mis-reported. What failed is **what the rule measured**. Width of the overlap interval, expressed as
a fraction of the ANSWER class's range, says how far the two classes' value ranges reach into each
other. It says nothing about **how many members of either class actually sit inside that reach**.
Those are different quantities, and on real data they come apart.

**Pre-registration discipline stands. The fix is a better pre-registered rule, not abandoning
pre-registration.** A criterion fixed before results is what made chunk 7.3's negative result
trustworthy; that property is not in question here and must not be weakened by this entry. What this
records is that a pre-registered rule can be *wrong in its construct* while being *right in its
discipline*, and the remedy is to pre-register a better one next time.

**The reductio — E2.** Signal E2 (minimum top-5 Jaccard under query perturbation) is **constant at
0.6667 across the entire positive class**: it takes two distinct values over 36 questions and cannot
discriminate anything by construction. Its overlap interval is therefore a zero-width point,
`[0.6667, 0.6667]`, which the width rule scores as **0.0% overlap — the best possible score.** That
interval contains **6 of 6 abstain and 22 of 30 answer questions**: near-total overlap by count,
scored as zero overlap by width. A signal constant on the positive class is the ideal case for a
width rule and the worst case for a screening rule. Width and concentration are different
quantities, and only concentration is evidence about separation.

The same defect, less starkly, spared chunk 7.3's three survivors: A5 elbow_index (17.9% width, **6
of 6** of the abstain class inside the interval), B4 max index gap (43.1%, **5 of 6**), A3 z_top1
(43.8%, **6 of 6**). A narrow interval is evidence of separation only if the classes are *outside*
it; a rule that never looks inside cannot tell "the classes barely meet" from "both classes are
piled into one narrow band."

**NOT retro-applied to chunk 7.3, deliberately.** The criterion that pass ran under was fixed before
any result was seen and **stands as run**. Changing a kill rule after seeing which signals it spared
is exactly the post-hoc move the discipline exists to prevent — and the fact that the amendment is
*stricter* does not exempt it: **a rule tightened after seeing which signals it spared is still a
rule fitted to data, and being stricter doesn't exempt it.**

**OBSERVATION (not a verdict).** All three of chunk 7.3's survivors — A5, B4, A3 — **would be DEAD
under the amendment**, each having more than half the abstain class inside its interval. E2 would be
dead by it too, having already been ruled NOT ESTABLISHED on independent grounds. This is recorded
as an observation about **the amendment's effect**, not as a re-verdict: chunk 7.3 §9's verdicts
stand as run, and A5/B4/A3 remain "survives screening, UNVALIDATED" in that pass's record. What the
observation does establish is that those three survivals are thin — none had CI-supported evidence
of discrimination in any direction — and any future reader of that promotion should read this entry
with it.

**Evidence:** `DevBrain/rag-reliability/passes/chunk73-artifacts/chunk73-REPORT.md` §13 (defect,
amendment, observation), §9 (verdicts as run), §7.1 (E2 as reductio);
`chunk73-signals.json` sha256 `a99e346c27e9e08de27d4df036b8e15a75458bb0878443da23ba68cae32ea51f`.

---

## 2026-09-02 — Chunk 7.3: perturbation stability is INVERTED — unanswerable queries sit in MORE stable neighbourhoods

**Decision:** query-vector perturbation stability is **rejected as an abstention signal**, and the
premise it was built on is recorded as **refuted, not merely unsupported**.

**The measurement.** Isotropic Gaussian noise at σ=0.20 of the query vector's L2 norm, 20 seeded
draws per question, exact search at k=40, retrieved-set overlap against the unperturbed run. On the
primary arm (vector + local reranker, 30 candidates, budgeted context):

| Signal | ANSWER (n=30) | ABSTAIN_UNANSWERABLE (n=6) | AUROC [95% CI] |
|---|---|---|---|
| E3 mean Jaccard @20 | **0.9160** | **0.9266** | 0.528 [0.278, 0.758] |
| E1 mean Jaccard @5 | 0.9333 | 0.8667 | 0.325 [0.133, 0.561] |
| E2 min Jaccard @5 | 0.6667 | 0.6667 | degenerate — 2 distinct values, constant on the positive class |

**Reasoning.** E assumed an unanswerable query sits in an unstable neighbourhood, so jittering it
would reshuffle the retrieved set more. E3 — the continuous member, no ceiling, range 0.8413–0.9905
— says the **opposite**: the abstain class is *more* stable. This is not weak separation, it is a
directional refutation. It is coherent with what a near-miss actually is: a question aimed squarely
at a real, densely-populated region of the corpus that happens not to contain the answer. A
well-populated neighbourhood is a robust one. The question being unanswerable is a fact about the
*text*, not about the geometry the query lands in.

E1 does point the intended way, but is DEAD on the pre-registered criterion (92.3% overlap) and its
apparent discrimination is carried by 8 ANSWER questions tied at the 1.0 ceiling — dropping them
moves AUROC 0.325 → 0.443 (toward chance) and worsens overlap to 100.0%.

**Scope: this generalises beyond this corpus.** Unlike the C2 finding recorded below, this does not
depend on how golden set v3 was constructed. The mechanism — a near-miss is by definition a query
that lands in dense, well-supported corpus territory — is a property of what "near-miss" means, not
of these six items. Any abstention design premised on "unanswerable ⇒ unstable retrieval" should be
treated as refuted until someone measures otherwise on a different corpus.

**Caveats.** n=6 on the positive class; every CI is wide and includes 0.5. The σ=0.20 sweep envelope
(6 probes) broke on the ceiling side at full scale — 0/6 probes predicted at the 1.0 ceiling, 8/36
observed. σ was **not** re-tuned after seeing that, deliberately.

**Evidence:** `DevBrain/rag-reliability/passes/chunk73-artifacts/` — `chunk73-REPORT.md` §8.2, §7.2,
§4.1; `chunk73-signals.json` sha256 `a99e346c27e9e08de27d4df036b8e15a75458bb0878443da23ba68cae32ea51f`.

### Control arm G — pre-registered expectation held

**Control arm G failed as pre-registered — 7.1's conclusion stands unamended.** Reranker score
*shape* (within-query z of top-1, top1−top2 margin, softmax entropy over the reranked scores) was
run as a labelled control with the pre-registered expectation that it fails. It failed: all three
DEAD (67.7%, 87.9%, 93.1% overlap), every CI including 0.5. **No void-convention entry against the
2026-08-2x wording of chunk 7.1's conclusion is required, and none should be written.** Reranker
score does not track groundedness; that conclusion is untouched by this pass.

One exploratory observation is fenced off and must not be read as contradicting the above: on a
separate, non-pre-registered class (answerable questions whose gold chunk the reranker itself pushed
below rank 5, n=8), G3 softmax entropy scored AUROC 0.795 [0.551, 0.977]. That asks whether the
cross-encoder is uncertain when it misranks — a self-consistency property — not whether its score
tracks groundedness. 34 comparisons were run without multiplicity correction, so ~1–2 such CIs are
expected by chance and exactly 2 appeared. Not promoted, not counted, not a finding.

---

## 2026-09-02 — Chunk 7.3: IDF-weighted rare-term absence measures PARAPHRASE DISTANCE, not unanswerability

**Decision:** pre-retrieval corpus-absence of rare query terms is **rejected as an abstention
signal on this corpus**, and the reason is recorded as **structural**, not as a tuning failure.

**The measurement.** `idf(t) = ln((N − ndoc(t) + 0.5)/(ndoc(t) + 0.5) + 1)`, N=260, with `ndoc` read
from `ts_stat('SELECT content_tsv FROM chunks')` — read-only, no index rebuilt, no new dependency.
C2 = max IDF among query lexemes with zero corpus occurrences.

C2 fires on **21 of 30 answerable** and **3 of 6 near-miss unanswerable** questions — *more often on
the negative class*. Overlap 100.0% of the ANSWER range; AUROC 0.400 [0.183, 0.600]. DEAD.

**Reasoning — why it cannot work here, by construction.** Golden set v3's near-misses were
*deliberately built to share vocabulary with the corpus*. u01's own authoring note records the
choice: overlap 3/7 = 0.43, "above the 0.30 line and left as-is deliberately… a question about one
named model's price cannot avoid naming it. Padding the question to dilute the ratio would game the
metric without reducing leakage." A rare-term-absence detector therefore **structurally cannot fire**
on the very items it is meant to catch.

What it fires on instead is answerable questions with unusual phrasing. a01 — *"Where did the person
who wrote this book go to university?"* — scores maximum corpus-absence, because both `univers` and
`wrote` are genuinely absent from the 260-chunk corpus (verified against an independent
`content_tsv @@ plainto_tsquery` count). Its authoring note says exactly why: *"Asked as 'go to
university'; the passage says 'alumnus'. No shared content word except the school name."* The signal
is measuring the gap between the asker's words and the corpus's words. That is paraphrase distance,
and on a golden set written to test paraphrase robustness it is anti-correlated with the thing we
wanted.

**Scope: this claim is limited to this corpus by construction.** It is *not* a general claim that
term absence can never signal unanswerability. It is the claim that on a golden set whose negatives
were engineered for vocabulary overlap, the signal is structurally blinded, and its apparent firing
pattern is an artifact of positive-item phrasing. A corpus with vocabulary-disjoint unanswerables
would be a different measurement.

**Secondary structural note.** Because `idf(0)` is a constant, C2 can only take two values (0.0 or
6.2577) — it is an indicator, not a continuous signal, and cannot be thresholded finely regardless.

**Evidence:** `chunk73-REPORT.md` §8.1, §5, §7; `chunk73-signals.json` (digest above); absence path
confirmed on a constructed nonsense term before any 0.0 was trusted.

---

## 2026-08-31 — RAG Reliability System Step 03 Chunk 7.2: exact brute-force vector search replaces Chroma/HNSW; six carried claims corrected

**Decision:** the vector arm is now exhaustive L2 search over a tracked 260x1024 float32 artifact
(`app/corpus/corpus_vectors.npy` + `corpus_vectors.json`, pinned by sha256
`dd4c3dd728f7dbb0770705f8b041444f89d86ec5d225ed30102d9f5699957100`). `ChromaVectorStore` and the
`chromadb` dependency are **deleted**. `ExactVectorStore` fails closed — a missing file, a digest
mismatch, a count != 260, a wrong collection, or a write attempt all raise `CorpusUnavailableError`;
there is no silent fallback and no network on the path.

**Adopted for DETERMINISM, SIMPLICITY and SPEED — explicitly NOT as a defect fix.** HNSW was not
losing anything that mattered: the measured miss rate is **0.135%** at k=40 and no miss ever sat
above exact-rank 15, so nothing inside the decision depth was ever dropped. What HNSW cost was
reproducibility. All 12 gate figures, all 18 jaccards and all 949 chunk 7 leaves are unchanged, so
the adoption cost was zero and the argument rests entirely on the three properties above.

|                                       | HNSW                               | exact                       |
| ------------------------------------- | ---------------------------------- | --------------------------- |
| cross-process stability, 36 questions | unstable on 7                      | **36/36 bitwise identical** |
| mean latency, k=40                    | 9.281 ms                           | **0.483 ms** (19x)          |
| p95                                   | 16.510 ms                          | 1.105 ms                    |
| per-process cold start                | 234.7 ms (index never checkpoints) | none                        |
| working set                           | index dir + WAL                    | **1.0156 MiB**              |

**ANN reintroduction threshold, measured not guessed: N ~= 4,000 (mean) / ~= 6,700 (p95)** with a
Python top-k cut, or ~= 5,900 / ~= 10,300 with `argpartition`. The corpus is 260 — 15-40x headroom.
Below that N, ANN is a pure cost. This is the number to re-check before anyone reaches for an index
again; it is machine-bound and was measured on this host.

**The distance is computed as `((M - q) ** 2).sum(axis=1)`, deliberately NOT the
`||a||^2 - 2a.b + ||b||^2` identity.** The identity form dispatches to BLAS gemv, whose accumulation
order varies with thread count — which is precisely the cross-process nondeterminism this change
exists to remove. Measured: the identity form produces a different `_PRE_RERANK_DIGEST`
(`3955f0dd...`) from the same ids and ranks. Do not "optimise" it back.

---

### Six carried claims, corrected. Original wording quoted; none of it deleted.

**1. Miss rate: stated ~15-25%, measured 0.135%.** Wrong by a factor of ~110-185x. At the real gate
parameter `candidate_k=30` it falls further, to **0.088%** (recall@30 = 99.912%). **Zero top-5
misses in 720 question-runs.** The estimate that justified treating this as a correctness problem
was never measured; when it was, the problem was two orders of magnitude smaller than the story.

**2. "u03 varies at rank 3" — FALSE.** u03 is stable: one distinct signature across 20 processes at
k=40 and at k=30, zero misses, and a top-5 **identical to exact search**
(`YPPR8B, Q56S9B, YZN3N6, C14MJC, 59CGNK`). No miss anywhere in the corpus sits above **exact-rank
15**. The claim both named the wrong question and asserted a depth that does not occur.

**3. The unstable set is `a02 a03 a15 a34 u02 u05 u06` — measured at 20 draws, and NOT exhaustive.**
The earlier handoff named `a03/a15/u02/u03`: three of four correct, `u03` wrong (see 2), and
`a02 a34 u05 u06` omitted entirely. It understated the blast radius _and_ overstated its depth.

**This set must not be stated as closed.** HNSW instability is probabilistic per process, so any
finite sample under-counts. Recorded because it happened: a 12-process control flagged an **eighth**
question, `u04`, which a second 12-process run then measured **stable (1 signature, 12/12)** — and
that second run showed only 5 of the 7 varying. `u04` was therefore **rejected as a 12-draw sampling
artifact, not accepted as an eighth member**. The corollary is the honest one: 12 draws is not
enough to enumerate the set, and 20 may not be either. **NOT ESTABLISHED: the complete membership.**
Nothing depends on it — the shipped test asserts stability, it does not enumerate instability.

**4. The gate metric is recall@30, not recall@20.** Read off the code:
`app/services/evaluation.py:38`, `RECALL_KS = (5, 10, 30)`; `arm_tuple()` composes
`(recall@5, recall@10, recall@30, mrr@10)`. Every "recall@20" reference in the carried notes is a
metric this project has never computed.

**5. `pre_rerank_pool_overlap` covers the 6 unanswerable questions, not 36.** Source:
`scripts/chunk5_benchmark.py`, `near_misses = [e for e in entries if not e.answerable]` — u01-u06,
3 arm-pairs each = **18 figures**. There is no per-question overlap record for the 30 answerable
questions, so any claim about "the 36-question overlap" refers to something that does not exist.

**6. The `16bcd06` carry-in understated the nondeterminism twice.** Original text, from
`docs/chunk7-scores.json` `manifest.measurement_choices.pool_nondeterminism` and the commit message
(both left unedited — they are measurement records with their own provenance):

> "the Chroma vector pool's **30th (candidate_k boundary) result** is not stable across processes"
>
> "a03's displacer list is **one sample of two orderings**"

Both understate it:

- **Not the boundary.** Missed exact-ranks across 20 processes at k=40 were **15, 19, 20, 24, 38,
  39, 40**; at `candidate_k=30` they were **15, 19, 20, 24**. The instability sits _inside_ the pool,
  not at its edge. Calling it a boundary effect made it sound like an artifact of where the cut
  falls; it is not.
- **More than two orderings.** `a03` does have 2 distinct signatures at k=40 (confirmed
  independently this pass), so that number was right _for a03_ — but `u02` has **5-6** across
  separate 20- and 12-process samples, and `u05` has 3. The understatement is that a03's two
  orderings were treated as characterising the phenomenon.
- **NOT ESTABLISHED:** a specific "three orderings at k=40" figure carried in the chunk 7.2 brief.
  This pass measured 2 for a03 and up to 6 for u02, and could not reproduce a 3. Recorded as
  unverified rather than repeated.

---

### Three further corrections found while doing the work

**7. `evaluation.py`'s "unrebuildable" claim was FALSE on both halves — and is fixed in code.**
Original comment above `GATE_FIGURES`:

> "The corpus is do-not-re-embed and unrebuildable — **the source PDF is gone and canonical text was
> never persisted**"

Verified this pass: the source PDF **is present** and its sha256 matches `documents.sha256` byte for
byte (`f6582a5529e8...0af7`, 18,537,673 bytes both), and the canonical text **is persisted** in
Postgres `chunks.text`, 260 rows. The corpus is **expensive** to rebuild — it costs a Voyage
re-embed — **not unrebuildable**. The comment is corrected in place; this entry records that it was
wrong and for how long. Three independent recovery paths now exist: the tracked `.npy` itself, the
PDF, and Postgres.

**8. `_PRE_RERANK_DIGEST` re-pinned, and the old value can never be regenerated.**
`3ffa60b3...89cfa3a` -> `03bc2840d2affc458d3adffdfcc613c0839b6ab7d14b2207439d46d1456d5e1f`. The old
comment instructed "regenerate it only by checking out 5db0925 and re-capturing" — **that
instruction is now impossible**, because 5db0925's vector arm was Chroma/HNSW and no longer exists
in this tree. What was measured before re-pinning: identical ids, ranks, membership and RRF scores
on all 8 baseline questions x 2 arms (`ANY id-order difference: False`); only the _string_ of
`vector_distance` moved, on 218 of 560 hits, by one float32 ULP.

**chunk 7.1g's `24885caf5c198313...` is NOT a valid expected value for anything.** This pass's exact
implementation produces `5e72ec7e4a18b0ff...` for the same un-quantized serialization — same ids,
same ranks, different accumulation order. **Two correct exact implementations differ by ~2 float32
ULP.** Any future digest must be re-derived from the implementation in the tree, never copied from a
pass report.

**9. What the 9dp quantization does and does not do — stated plainly, because the obvious reading is
wrong.** `_quantize_distance` cuts `vector_distance` to 9dp before hashing. It removes sensitivity to
representation **below 1e-09 only**. It does **NOT** absorb float32 accumulation noise, which reaches
**~6e-07** here — wider than the grid and wider than the tightest adjacent distance gap
(3.58e-07 measured; 1st percentile 4.01e-05 across the 36 top-40 lists). Three exact searches
agreeing on every id and rank still produce three different digests (float32 `03bc2840`, float64
`6b773af8`, BLAS-identity `3955f0dd`).

> **The digest is reproducible because `ExactVectorStore` is bitwise deterministic, not because the
> field is quantized.** Anyone who reads the quantization as the source of reproducibility will
> re-introduce a BLAS path and be surprised.

---

### Verification, and what was deliberately left alone

**The cross-process test is real, and that was demonstrated rather than asserted.**
`tests/test_vector_search_stability.py` spawns **12 separate OS processes** — not an in-process loop,
which is structurally blind here because the old defect was fixed at index-load time — and asserts
bitwise-identical `(id, rank, distance.hex())` for **all 36 questions at k=40**, naming
`a02 a03 a15 a34 u02 u05 u06` individually in its output. Pointed at the old Chroma/HNSW path it
**fails and exits 1**, flagging all seven. That negative control is not shipped (it would re-import
the deleted dependency); the `_build_store()` seam that made it a 30-line external file stays.

**The digest gate's blind spot, now measured.** `_BASELINE_QIDS` is
`a01 a05 a09 a13 a17 a21 a25 u01`; the unstable set is `a02 a03 a15 a34 u02 u05 u06`. Across the same
12 HNSW processes: **`BASELINE qids unstable: 0/8`, overlap with the unstable set: `[]`**. The two
sets are **disjoint**, so `_PRE_RERANK_DIGEST` could not have caught this on any run — which is
exactly why it never did. A gate whose fixture excludes every unstable case is not a gate for that
property.

**The 10-leaf `docs/chunk7-scores.json` drift is PRE-EXISTING and was left alone.** Not inherited as
a claim — re-verified this pass by running the script under both backends:

```
exact vs committed : 10 differing leaves
hnsw  vs committed : 10 differing leaves
exact vs hnsw      :  0 differing leaves     <- this change's own effect
same leaf set? True
```

**It is ONE event, not eight.** Every one of the 7 `percentile_in_c_non_gold` shifts is exactly
`1/128 = 0.0078125` — one element of the 128-member non-gold population crossing a rank boundary —
and the same crossing moves the population-c median (-6.119887 -> -6.143766) at its 3 recording
sites. The committed artifact records `git.head = d574409` with `dirty: true` against a current HEAD
of `16bcd06`, so it was generated from a dirty tree one commit back. Out of scope, not chased, file
not regenerated.

**Build determinism was unreachable in `chromadb 1.5.9` — CARRIED, NOT RE-VERIFIED.** The finding
that 5 rebuilds under every public knob pinned produced 5 distinct graphs, and that
`RAYON_NUM_THREADS` is genuinely read (17 -> 10 workers at `=1`) while `OMP_NUM_THREADS` was never
shown connected to anything, comes from passes **chunk71e and chunk71f, which no longer exist** (see
below). It is recorded because it is load-bearing for "why not just fix HNSW", and flagged because
this pass could not re-run it: `chromadb` is uninstalled as of this entry, so re-verifying would
require re-adding the dependency being removed. **The important half of it is the epistemics, and
that half is safe to keep:** the earlier "no process correlate" conclusion was drawn from
`OMP_NUM_THREADS`, a variable never verified to be connected to anything — a negative result about
an unconnected knob is not a negative result about the system.

**Pass reports `chunk71e` and `chunk71f` were lost to a `/tmp` clear** and no rebuild path for them
exists anywhere. Their load-bearing conclusions survive only via **chunk 7.1g's independent
re-verification**, which re-derived the unstable set empirically rather than trusting the carried
list — and in doing so caught corrections 2 and 3 above. That report is saved outside `/tmp`, at
`DevBrain/rag-reliability/passes/chunk71g-REPORT.md`. **The lesson is procedural: a pass whose only
artifact lives in `/tmp` has not been recorded.**

**Cross-references.** Supersedes the vector-backend half of **2026-08-19 — "Step 02 Chunk 5: hybrid
retrieval fused with RRF"** (RRF, `RRF_K=5` and the MRR regression are untouched; only the store
behind the vector arm changed). Corrects the `pool_nondeterminism` record in `docs/chunk7-scores.json`
and commit `16bcd06`, both left unedited. Leaves **2026-08-23 — "the chunk 5 block is VOID"** and
**2026-08-23 — "DoD #5 answered, SPLIT verdict"** intact: all 12 gate figures and all 18 jaccards
reproduce exactly, so nothing those entries rest on moved. The 7.1 conclusion — reranker score does
not track groundedness, 7.2 still blocked — **HOLDS** under exact search with every true neighbour
present: 26/30 gold and 124/128 non-gold still fall inside the shared region [-10.745, +1.690] and
the stop condition still fires **both** low and high.

---

## 2026-08-25 — RAG Reliability System Step 03 Chunk 7 Phase E: `HeuristicCharCounter` demoted, `TiktokenCounter` is now the budget-safety mechanism

**Decision:** `app/services/context_budget.py` gains `TiktokenCounter` (exact tiktoken `cl100k_base`
counts, times `CROSS_TOKENIZER_SAFETY_FACTOR = 1.2`) and makes it `plan_context`'s default counter.
`HeuristicCharCounter` stays available — nothing deleted — but is no longer what makes the budget
safe. This closes out the investigation the 2026-08-24 correction (above) opened: that entry
retracted the `PER_CHUNK_OVERHEAD_TOKENS = 96` measurement claim and reported the heuristic's
tiktoken-measured margin; Phase C characterised _why_ it under-counts; Phase D sized how bad the
real floor is; this entry is where the fix actually lands.

**Why `HeuristicCharCounter` had to be demoted, restated in one place.** Three independent
measurements, escalating:

| stage                               | reference            | min margin  | what it means                                                                       |
| ----------------------------------- | -------------------- | ----------- | ----------------------------------------------------------------------------------- |
| original claim (2026-08-24 chunk 6) | WordPiece            | 1.0524x     | never under-counts — the _only_ property ever proven                                |
| Phase B re-measurement              | tiktoken cl100k_base | **0.9722x** | under-counts on 4 of 260 real corpus chunks                                         |
| Phase D synthetic probe             | tiktoken cl100k_base | **0.6257x** | a hand-built mixed block (glued numerics + short lines) undercounts by over a third |

The corpus's observed floor (0.9722x) is the edge of a real cluster, not an isolated outlier (Phase
D: four chunks under 1.0, a fifth at exactly 1.0000, then a slow climb — no gap). The synthetic
floor (0.6257x) shows the corpus itself understates the risk: it doesn't happen to contain a chunk
that's _purely_ identifier-dense the way a model-comparison table or package list would be.

**The two named mechanisms (Phase C), both confirmed to generalize (Phase D):**

1. **Punctuation-glued numeric compounds** — numbers stuck directly to `$`, `%`, `,`, or `-` with no
   space (`$0.50`, `500,000-token`, `95-99%`, `all-MiniLM-L6-v2`). cl100k_base's BPE fragments these
   into many short tokens (`all-MiniLM-L6-v2` → 10 tokens for 16 chars) that the heuristic's flat
   3.5-chars/token rate doesn't charge for. Corpus-wide: chunks with 10+ such tokens (14 of 260)
   average margin 1.1633 vs ~1.38 corpus median — near-misses that don't cross 1.0 still cluster low,
   confirming the mechanism is real, not coincidence.
2. **Short newline-delimited list/TOC layout** — many short lines (page numbers, section titles),
   each newline and each 2-3 digit number costing a full BPE token regardless of length. Corpus-wide:
   chunks that are >60% short lines (9 of 260) average margin 1.0945. This mechanism explains the
   single _worst_ offender (margin 0.9722, zero glued-numeric tokens) — the two mechanisms are
   genuinely distinct, not one feature wearing two names; forcing them into one category would have
   been tidying up the finding.

**Known gaps — content types never probed, stated as gaps rather than assumed safe.** The corpus is
one RAG/vector-DB book; the synthetic probes were hand-built to mimic patterns already in it, just
denser. None of the following were tested and could sit lower than 0.6257x: **code blocks, URLs,
citation/reference lists, CJK-adjacent text**, heavily abbreviated scientific notation, or markdown
tables with many narrow columns. `TiktokenCounter` does not need this gap closed to be safe (it
counts the real text, whatever it contains) — the gap matters only for anyone tempted to reach for
`HeuristicCharCounter` again outside prose.

**`CROSS_TOKENIZER_SAFETY_FACTOR = 1.2` — what it covers and what it explicitly does not.** It
covers cross-tokenizer drift **among BPE-family models only**: cl100k_base is a proxy for whichever
tokenizer the eventual generation model actually uses (unpinned until Chunk 7+ settles it), and
different BPE vocabularies can count the same text differently even when both are "real"
tokenizers. It does **not** cover structural fragmentation — that risk is already retired by using a
real tokenizer's real count on the real text, not by a multiplier. **Provisional**: 1.2x is a round,
unmeasured margin, chosen because there is nothing to measure the drift against until the
generation model is pinned. Revisit then, not before.

**Where it's applied.** Inside `TiktokenCounter.count()` itself (`ceil(raw_tiktoken_count * 1.2)`),
not as a separate step inside `plan_context`'s cost loop. This was a deliberate scope decision: an
earlier draft applied the factor uniformly to whichever counter ran, but that would have required
recalculating every hardcoded fixture in `_check_offline` (which exercises `HeuristicCharCounter`
explicitly, calibrated to the un-inflated numbers) for no benefit — `HeuristicCharCounter` isn't the
safety mechanism the factor is protecting anymore, so inflating its output too would only have
changed unrelated, already-passing self-check numbers without covering any real risk.
`counter.name` for `TiktokenCounter` includes the factor in its string
(`tiktoken-cl100k_base-v1+1.2x-cross-tokenizer`) so the manifest stays honest about what actually
ran.

**Cache resolution is now internal to `TiktokenCounter`, not left to a caller-set env var — a real
defect Phase E verification found and fixed.** Proven under genuine network-namespace isolation
(`unshare -rn`, confirmed with a control `curl` that network really was blocked): with
`TIKTOKEN_CACHE_DIR` set correctly, `tiktoken.get_encoding("cl100k_base")` loads from cache with
zero connection attempts. **Without it set, the same call attempts a real HTTPS connection and fails
loudly under isolation** — the exact defect flagged as a risk in Phase A/B, now confirmed and fixed:
`TiktokenCounter.__init__` sets `TIKTOKEN_CACHE_DIR` itself and additionally blocks
`socket.getaddrinfo`/`socket.socket.connect` for the duration of the load, so any future caller who
forgets to set the env var gets a named `TiktokenCacheMissing` instead of a silent network fetch —
the guarantee no longer depends on remembering to set anything.

**`usable_budget` is unchanged — 193,488 — and saying otherwise would be wrong.** It is pure reserve
arithmetic (`context_window - prompt_scaffold_reserve - query_reserve - answer_reserve`), independent
of which counter runs; no counter change can move it. What actually changed, re-derived and reported
here: the corpus's worst-case single-hit cost. Under the retired `HeuristicCharCounter`, 675 tokens.
Under `TiktokenCounter` (the real mechanism now), **584 tokens** — lower, not higher, because the
corpus's single _longest_ chunk (1999 chars, `CORPUS_MAX_CHUNK_CHARS`) happens to be ordinary prose,
not one of the fragmentation-prone short/dense chunks Phase C/D found (the worst _fragmentation_
offender is 365 chars, nowhere near the longest). Both fit `usable_budget=193,488` with enormous
headroom (192,904 tokens) — the budget was never close to binding at default reserves, before or
after this change; this is insurance, not a fix for an active problem (same framing as the original
chunk 6 entry's "does not bind at default reserves" note).

**`plan_context`'s "no default, ever" stance, deliberately reversed, not quietly dropped.** The
original reasoning (chunk 6) was that any implicit default was equally untrustworthy among options
with no clear winner. That is no longer true: `TiktokenCounter` is the validated mechanism, and
`counter_name`/`budget_manifest` still always record which counter ran, so a default cannot silently
go untracked — the concern the original stance was protecting against is still satisfied, just by a
different mechanism (recording, not refusing).

**Verified before implementing, not assumed:** re-ran `_check_offline` (unchanged, `HeuristicCharCounter`
fixtures unaffected by scoping the safety factor inside `TiktokenCounter`), the live corpus check
(new: `TiktokenCounter` worst-case-hit assertion, passing), `ruff` and `mypy --strict` clean, the
12-figure regression gate (PASS, all 12 exact), and `_PRE_RERANK_DIGEST` (byte-identical to
`5db0925`) — none of this touches retrieval or evaluation code paths, confirmed rather than assumed.

**The blocking prerequisite for chunk 7's provenance renderer (`ProvenanceHeaderUnmeasured`,
added in the 2026-08-24 correction) still fires**, unaffected by this change — confirmed by running
`uv run python -m app.services.context_budget`: every check above passes and prints, then the module
still exits 1 on the unbuilt-renderer check, as designed.

---

## 2026-08-24 (correction) — RAG Reliability System: chunk 6's PER_CHUNK_OVERHEAD_TOKENS=96 measurement claim RETRACTED; heuristic counter margin re-measured against tiktoken cl100k_base and DOES NOT hold

**This corrects two specific claims in the 2026-08-24 chunk 6 entry below, produced by chunk 7
Phase A/B (`scripts/fetch_tiktoken_cache.py`, `scripts/validate_token_counter.py`). Nothing else in
that entry is affected — whole-chunk selection, the five-reserve design, `score_source`, the
tie-break, and the exclusion accounting all stand as recorded.**

**Claim 1 RETRACTED — "the 96 is MEASURED, not asserted."** It was not. Phase A audited where the
five header-format figures (23/40/49/69/74 reference tokens) actually live: a comment in
`context_budget.py` and a table in the chunk 6 entry below, both prose, neither backed by a script.
Worse, the table itself only gives a literal string template for 2 of the 5 rows
(`[filename | page N | chars a-b]` and `[doc_id=... page=... chars=a-b]`); "markdown block",
"XML-ish `<source ...>`", and "JSON line" are named, not spelled out. And the renderer the whole
figure is supposedly measured against **does not exist** — that entry itself calls it "chunk 7's
renderer" in the future tense. A number described as measured, where the artifact being measured
was never built and 3 of 5 inputs were never written down, is not a measurement.

**`PER_CHUNK_OVERHEAD_TOKENS = 96` is reclassified: UNVALIDATED ESTIMATE, not a measured figure.**
The value is unchanged — nothing in this correction says 96 is wrong, only that the claim it was
_measured_ is false. It stays in force as a working estimate until it can be measured for real. See
the blocking prerequisite below.

**Claim 2 CORRECTED — "min margin 1.0524x."** That figure is real but was measured against the
vendored WordPiece tokenizer, and this entry's own text already flagged it as non-universal on
ID-dense text. `scripts/validate_token_counter.py` re-measures the same property against **tiktoken
cl100k_base** — a same-family BPE tokenizer, chosen as a **PROXY** reference because the generation
model is still unpinned (chunk 7+), not because it is the tokenizer that will actually bill this
system. Measured 2026-08-24, all 260 live corpus chunks:

**min margin 0.9722x, median 1.3820x, 4 of 260 chunks under-counted.**

**This is the finding, reported without reconciling it against 1.0524x, per the instruction that
produced this correction.** The heuristic's "never under-count" property (Decision A in the entry
below) does **NOT** hold against tiktoken cl100k_base. It only ever held against WordPiece. Worst
three offenders, all "table of contents"-style text — short numeric fragments packed into an
otherwise-prose chunk:

| margin  | chunk                            | heuristic | reference | excerpt                                                                                                                    |
| ------- | -------------------------------- | --------- | --------- | -------------------------------------------------------------------------------------------------------------------------- |
| 0.9722x | `chk_01M0D4BMG6AASVJBVVD32X3B4S` | 105       | 108       | "Contents 245 05 03 Glossary About The Author Preface 06 27 61 106 151 176 208 Chapter 01: Is RAG Dead? Chapter 02: How t" |
| 0.9829x | `chk_01M0D4BMGWR72JGSFZ9V1XCQ6F` | 345       | 351       | "Chapter 05 How to Select a Vector Database Table 5.2 compares index structures across key dimensions: Index Type Recall " |
| 0.9901x | `chk_01M0D4BMGRY2JMWDR5MX7ER72H` | 201       | 203       | "Chapter 04 How to Select an Embedding Model Early embedding models limited inputs to 512 tokens, and this forced long d"  |

**The prose/ID-dense split, defined for the first time by this measurement, MISSED the failure
mode.** Chunk 6 recorded no operational rule for "ID- and number-dense text," only prose describing
it. `validate_token_counter.py` introduces one — a chunk counts as ID-dense when more than 15% of
its characters are digits — and flags it explicitly as newly introduced, not inherited. Under that
rule, **0 of 260 chunks classify as ID-dense, and all 4 under-counting chunks classify as prose.**
The rule as stated does not isolate the actual failure mode (short numeric runs embedded in
longer prose, not digit-dominated text) — recorded honestly rather than tuned after the fact to
produce a cleaner split.

**Consequence: this changes the guardrail work.** `CHARS_PER_TOKEN = 3.5` in `context_budget.py`
was chosen because it sat below the WordPiece floor (3.675) with margin. It does not sit below the
tiktoken floor implied by this run. Before chunk 7's Guardrail can trust `ChunkExceedsBudget`'s
invariant against a BPE-family generation model, either `CHARS_PER_TOKEN` must be lowered and
re-validated against tiktoken, or `HeuristicCharCounter` must be replaced by a tiktoken-backed
counter for that regime — not decided here, only surfaced.

**Reproducible now, where it was not before.** `scripts/fetch_tiktoken_cache.py` pins and caches
tiktoken's cl100k_base BPE file (content-addressed, checksum-verified, `models/tiktoken/` gitignored
same as `models/reranker/`). `scripts/validate_token_counter.py` re-runs the margin measurement
above and, once chunk 7's renderer exists, the header-format one (`--skip-headers` was passed this
run precisely because that renderer still does not exist — see the blocking prerequisite in
`app/services/context_budget.py`, `ProvenanceHeaderUnmeasured`).

**Incident, corrected in-flight and left on record rather than smoothed over:**
`scripts/fetch_tiktoken_cache.py`'s `EXPECTED_SHA256`, written from memory during Phase A because
Phase A could not import tiktoken to read it, had 8 hex digits wrong. The download-then-verify path
caught it on the first real run: refused to cache the mismatched bytes, deleted the `.part` file,
exited nonzero. Corrected against the installed `tiktoken==0.8.0` package's own source before the
cache was written. The failure mode worked exactly as designed — loud, not silent.

---

## 2026-08-24 — RAG Reliability System Step 03 Chunk 6: context budgeter — tokens not characters, whole chunks only, and Decision B is FIVE reserves not four

> **Two claims in this entry are corrected — see "2026-08-24 (correction)" directly above.**
> (1) "The 96 is MEASURED, not asserted" is RETRACTED; `PER_CHUNK_OVERHEAD_TOKENS = 96` is now an
> UNVALIDATED ESTIMATE, not a measured figure — no renderer existed to measure it, and 3 of the 5
> header formats below never had a literal template on record. (2) "min margin 1.0524x" was
> measured only against WordPiece; measured against tiktoken cl100k_base (a proxy for the still-
> unpinned generation model) the min margin is 0.9722x and 4 of 260 chunks under-count — the
> "never under-count" property does NOT hold universally. Everything else below — whole-chunk
> selection, the five-reserve design, `score_source`, the tie-break, the exclusion accounting — is
> unaffected and unchanged. Annotation only; the entry below is otherwise unchanged.

**Decision:** `app/services/context_budget.py` packs reranked hits into a token budget and records
what it dropped. `plan_context(hits, budget, counter, order)` selects **whole chunks only**, greedy
by score descending, and returns `BudgetedContext(included, excluded, score_source, budget,
counter_name, order, tokens_used, tokens_remaining)`. Nothing is wired into the CLI — there is no
generation step to feed until chunk 7. The 12 frozen `GATE_FIGURES` reproduce byte-identically
before and after (verified both directions this session), and `retrieval._PRE_RERANK_DIGEST` still
matches commit `5db0925`.

**Reasoning:** the budgeter was recorded as carried-forward-not-dropped at Step 02's close
(2026-08-20 entry below) — "Step 03 or later must build it before an LLM call receives unbounded
context." There is no generation step and no Verifier yet, so the DoD is **structural**, not
metric-based: determinism, whole-chunk integrity, and an auditable exclusion record. Proposing
golden-set metrics for this chunk would have measured nothing.

**Budget is in TOKENS, and the counter is injected.** `TokenCounter` is a Protocol
(`name`, `count(text) -> int`). One implementation ships: `HeuristicCharCounter`
(`heuristic-char-3.5-v1`) = `ceil(ascii_chars / 3.5) + non_ascii_chars`. Offline, no new
dependency, no network. `3.5` sits below the measured chars-per-token **floor of 3.675** across all
260 corpus chunks, which is what makes the over-estimate hold with margin rather than by luck. The
non-ASCII term is inert on this corpus (291 such characters) and costs one line, but it closes the
regime — CJK, where BPE runs near one token per character — in which a pure chars/N ratio
under-counts badly. A rule that is only conservative for Latin text does not satisfy "never
under-count."

**The property is TESTED, not asserted.** `_check_never_undercounts` encodes all 260 live corpus
chunks with the **vendored reranker tokenizer** (`models/reranker/tokenizer.json`,
`cross-encoder/ms-marco-MiniLM-L6-v2`, bert-base-uncased WordPiece) and fails if the heuristic ever
comes in under it. Measured **min margin 1.0524x**, median 1.49x, max 1.77x. This was obtainable
offline because `tokenizers>=0.20` is already a declared dependency and the weights are already
verified against `scripts/reranker_model.sha256` — no network, no new dep, `$0`.

**Two honest limits on that property, both recorded rather than smoothed over.** (1) WordPiece is
**not the generation model's tokenizer**, which stays unpinned until chunk 7+. It is the
_pessimistic_ reference for English prose — WordPiece fragments harder than BPE — so it bounds the
error on this corpus, but it is not proof against BPE in general. **When the tiktoken-backed counter
arrives, `_check_never_undercounts` MUST be re-run against it.** (2) The margin holds on **prose**,
not universally: ID- and number-dense text fragments far harder — a JSON provenance header measured
44 heuristic tokens against **74** reference tokens. This is designed around, not ignored: the
counter is only ever applied to chunk text, and the per-chunk render overhead is sized against the
reference tokenizer instead of against the heuristic.

**`chunking.py`'s `token_estimate` must never be reused as the budget counter.** It stores
`max(1, len(text) // 4)`, an embedding-sizing estimate against a 32k context with a 64x margin —
correct for that job. Measured against the reference tokenizer it **under-counts on 3 of 260
chunks**. Left unchanged, with a comment at the assignment site so a future reader does not wire it
into a budget and silently overrun the context.

**Decision B is now FIVE reserves, not four — this deviates from B as originally specified, and is
recorded as a deviation rather than quietly absorbed.** The original four:

| value                     | default | reasoning                                                                                                                                                                              |
| ------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CONTEXT_WINDOW`          | 200_000 | claude-sonnet-5, matching `Settings.llm_model`'s default. If that default moves this must move with it — a budget sized for a window the model does not have looks correct and is not. |
| `PROMPT_SCAFFOLD_RESERVE` | 2_000   | system prompt, citation-format instructions, inter-chunk delimiters — the _fixed_ scaffold, independent of how many chunks are selected.                                               |
| `QUERY_RESERVE`           | 512     | a ceiling on the question, not a measurement of one. Golden-set questions run 40-160 characters; 512 tokens is ~1,800 characters, an order of magnitude of headroom.                   |
| `ANSWER_RESERVE`          | 4_000   | a truncated answer is a wrong answer, and the failure is invisible to the Verifier — it scores what was produced, not what was cut off.                                                |

The fifth, **`PER_CHUNK_OVERHEAD_TOKENS = 96`**, is the provenance header chunk 7's renderer will
emit per included chunk so the model can cite. Without it the assembled prompt exceeds the budget by
`n_hits x header` — a real overrun, not a rounding concern. Folding it into
`PROMPT_SCAFFOLD_RESERVE` instead was rejected: the reserve would then silently depend on `top_k`
and be wrong the moment `top_k` changed.

**The 96 is MEASURED, not asserted.** Five plausible header formats rendered at this corpus's widest
real values (page 247, `char_end` 271256, a 33-character filename, a 30-character ULID chunk id):

| format                              | heuristic | reference |
| ----------------------------------- | --------- | --------- |
| `[filename \| page N \| chars a-b]` | 20        | 23        |
| `[doc_id=... page=... chars=a-b]`   | 20        | 40        |
| markdown block                      | 37        | 49        |
| XML-ish `<source ...>`              | 43        | 69        |
| JSON line                           | 44        | **74**    |

96 sits above the observed reference maximum of 74, with room for a format chunk 7 has not chosen.
Sized against the **reference** tokenizer deliberately — the heuristic scored those same headers at
20-44, i.e. it under-counts exactly this kind of text.

**Decision C's invariant tightens accordingly.** A single hit violates the budget when
`chunk_tokens + per_chunk_overhead_tokens > usable_budget`, not `chunk_tokens` alone. It raises
`ChunkExceedsBudget` naming the chunk, its true cost, the text/overhead split, all four component
values, the counter, and the fix. Never a silent drop: whole-chunk inclusion means there _is_ no
correct way to fit an oversized chunk, because truncating it would break the `char_start`/`char_end`
spans chunk 9's Verifier scores against — the Verifier would validate groundedness against evidence
the model never saw. That is a correctness constraint, not a preference. Asserted early against the
real corpus, not against the chunker's config: `CORPUS_MAX_CHUNK_CHARS = 1999` is a measured
constant and `_demo()` re-reads the live maximum and fails if it moved — the same discipline as
`GATE_FIGURES` and `_PRE_RERANK_DIGEST`.

**Corpus measurement, 2026-08-24, `doc_01M0D39WZDYY7STA3PHWT5R4C7`, 260 chunks:**
chars min 69 / median 1060 / p95 1870 / **max 1999** / mean 1051.9; reference tokens
14 / 203 / 344 / **395** / 203.7; chars-per-token min 3.675, median 5.200, max 6.194.

**`score_source` is a FIRST-CLASS field on the return type, not an internal detail.**
`NoOpReranker` returns `scores={}`, so every hit on the no-rerank path — the path the frozen gate
and the CLI's `--reranker none` degradation switch both use — has `rerank_score is None`. Requiring
a reranked `Retrieval` would have made the budgeter unusable in exactly the configuration the
benchmark runs in. So the selection key falls back to `rrf_score`, and which one was used travels on
the result. **It must be uniform across a single `Retrieval`**: a mixed set raises
`MixedScoreSources`, because a raw cross-encoder logit (unbounded, ~±10) and an RRF artifact
(~0.01-0.03) on one sort key would silently rank by _score source_ rather than by relevance,
putting every reranked hit above every un-reranked one regardless of merit. Homogeneity is what
makes the fallback sound; this is not a general comparator across score types.

**Why that field matters downstream — cross-reference to the 2026-08-19 chunk 5 entry below.** That
entry records that RRF fuses **by rank position only** and is structurally incapable of signalling
absence: an RRF score of 0.21 means "was ranked", not "is weakly relevant". Chunk 7's Guardrail
thresholds abstention on reranker score. It therefore has to be able to see `score_source == "rrf"`
and **refuse to calibrate on it** — which it can only do if the field is carried rather than
reconstructed from `rerank_score is None`. Read this entry and the 2026-08-19 one together.

**Selection is greedy by score, descending, tie-broken by `chunk_id`.** The tie-break matches
`reranking._rank()` and `_LEXICAL_SQL`'s `ORDER BY score DESC, c.id` — one convention repo-wide, so
equal scores never resolve differently depending on which stage sorted them. Greedy-by-score is
knapsack-suboptimal in general and correct here: chunk sizes are tightly clustered (69-1999 chars),
relevance is **not additive** — the rank-1 chunk carries most of the answer — and a density-greedy
pack can drop the top hit when it happens to be long, the one outcome a RAG context must never
produce. Presentation order is a separate axis with exactly one member
(`PresentationOrder.RERANKER_ORDER`); coherence reordering and lost-in-the-middle placement are
unmeasured and there is no harness to evaluate them until generation exists.

**A reachability finding, surfaced by the tests rather than by design.** `included` empty with
`excluded` non-empty is **unreachable**, and that is a guarantee, not an oversight: decision C makes
an unfittable single hit an _error_, so every hit fits alone, so the greedy loop always includes at
least the top-ranked one. The Guardrail therefore reads "something relevant was budgeted out" off
**`excluded` being non-empty**, never off `included` being empty — and it can rely on the best hit
never having been the one dropped. The docstring's table was corrected to say so; the original
three-way framing overstated it.

**At default reserves the budget does not bind.** `usable_budget` = 200000 − 2000 − 512 − 4000 =
**193,488**; the worst-case hit costs **675** tokens; at `top_k=5` that is under 2% of budget. The
exclusion path is therefore **proven structurally, in `_demo()` with deliberately small budgets —
not measured in production**. It must not be reported as the latter. The budgeter is insurance for a
smaller pinned model, a larger `top_k`, or full-pool packing.

**Module constants, not `Settings` fields — deliberate.** The generation model is unpinned until
chunk 7+, so there is no real context window to configure and an env surface for values nothing sets
is a surface that drifts. `ContextBudget` validates itself in `__post_init__` (every value
non-negative; `usable_budget > 0` or `ImpossibleBudget`) so an impossible budget fails **where the
numbers are written**, not by silently returning an empty selection that a call site cannot
distinguish from "retrieval found nothing". `usable_budget` is a derived property so the arithmetic
has exactly one definition.

**Manifest.** `budget_manifest()` lives in the budgeter — one definition, the same reason
`read_manifest` lives in `reranking.py` rather than in the fetch script — and is wired into
`scripts/chunk5_benchmark.py::_manifest()` beside `corpus.document_id` and the reranker revision. It
records the counter identity, all five values, and the derived `usable_budget`, so a budgeted run is
reconstructible from the manifest alone. **`docs/chunk5-results.json` was NOT regenerated** —
re-running that script would rewrite a frozen results file; the block lands in the next run.

**Tests are in-module `_demo()` asserts, not pytest** — `tests/` holds only fixtures, every module
in this repo self-checks this way, and installing pytest is an approval gate. Run
`uv run python -m app.services.context_budget`. Ten checks: the never-under-count property over the
live corpus, the corpus-max invariant, `ChunkExceedsBudget` with a message that names the numbers,
byte-identical determinism across repeat runs, tie-break stability under input reversal, whole-chunk
object identity (`included` elements are the _same objects_ passed in — not copies, not truncated),
the empty/non-empty exclusion distinction, `MixedScoreSources`, `ImpossibleBudget`, and the manifest
block round-tripping through JSON.

**Regression gate: PASSED, byte-identically.** All 12 frozen figures reproduce —
lexical `0.233 0.400 0.567 0.142`, vector `0.833 0.900 1.000 0.700`, hybrid
`0.667 0.867 0.967 0.445` — captured before the change and re-run after. `ruff` and `mypy --strict`
clean across 44 files. Nothing was committed or pushed.

**NEW CHUNK 7 PREREQUISITES — logged here, listed in SCRATCHPAD, links both ways.**

1. **The renderer's actual provenance-header format must fit within `PER_CHUNK_OVERHEAD_TOKENS`
   (96), and chunk 7 must ASSERT that it does** — the budget charges for it whether or not it is
   true. Sits alongside the existing `resolve_gold_chunk_ids` / `gold_by_id` -> `target_chunk_ids`
   rename (which this chunk verified it does **not** touch: the budgeter consumes `RetrievedChunk`
   and never reads the golden set, so the rename was **not** a prerequisite here).
2. **Re-run `_check_never_undercounts` against the tiktoken-backed counter** once the generation
   model is pinned.
3. **Consider promoting the five constants to `Settings`** once the real context window is known.
   Recorded so the module-constant choice is _revisited_ rather than inherited by default.
4. **The Guardrail must branch on `score_source`** and refuse to calibrate abstention thresholds on
   `"rrf"` — see the 2026-08-19 entry on RRF's structural inability to signal absence.

---

## 2026-08-23 — RAG Reliability System: KNOWN LIMITATION of golden set v3's near-miss set — u06's designated lure is not the lure retrieval produces

**Status: LOGGED, not fixed. Golden set v3 stays FROZEN.** No fixture was edited and none may be
edited to act on this. Review it when the near-miss set is next revised.

**The limitation.** u06 asks: _"What is the per-document price of the technique that prepends a
generated summary before embedding?"_ v3 designates the **p95 agentic-chunking pricing chunk**
(`chk_01M0D4BMGPC4TSS179JBQE32HB`) as its `near_miss_to` lure. But retrieval consistently ranks
the **p90 chunk describing the technique the question names** (`chk_01M0D4BMGPTW90AQ81EM4R32VW` —
"prepend this context to the chunk before embedding") **above it, in every arm that pools the
designated lure at all.**

**Observed ranks of the designated lure, measured 2026-08-23 (read-only, $0):**

| arm     | pre-rerank      | post-rerank |
| ------- | --------------- | ----------- |
| lexical | **not in pool** | —           |
| vector  | 2               | **6**       |
| hybrid  | 8               | **6**       |

Meanwhile the p90 technique chunk is at **rank 1 in all three arms**.

**Why this is a fixture limitation and not a retrieval fault.** Retrieval is behaving sensibly: the
question names a technique, and the p90 chunk is the passage that describes that technique. The
plausible-but-wrong passage a system would actually be tempted to answer from is the one retrieval
surfaces, not the one the fixture nominates. So **v3 describes a lure the system does not fall
for, and omits the one it does.** The `why_unanswerable` reasoning behind u06 is still correct —
the corpus genuinely never prices context-enriched chunking — it is the _choice of near-miss span_
that does not match retrieval's behaviour.

**Cost of the limitation.** u06 is UNSCORED in every arm at `ABSTENTION_TOP_K = 1`, so its
abstention behaviour is never exercised. **That is one item in six — 17% of the abstention
fixture — contributing no signal at all**, and it is why coverage reads 5/6 rather than 6/6 in
every configuration measured.

**Explicitly NOT the chunk 7 open question.** The reranker moving this lure from rank 2 to rank 6
is _not_ evidence that the cross-encoder demotes correct chunks — this chunk is a lure on an
unanswerable question, not a correct answer. That open question is recorded separately and is
fitted only on the answerable set. Conflating them would corrupt chunk 7's threshold calibration.

**On revision, the candidate change** (to be decided then, not now): re-designate u06's
`near_miss_to` to the p90 technique chunk, which is the passage retrieval actually surfaces and
therefore the one an abstention decision would actually have to resist. **That is a fixture edit
and a re-baseline** — it changes what u06 measures, so it voids u06's recorded coverage and must
be recorded as such in the entry that makes it.

**Cross-reference:** the run that surfaced this is **2026-08-23 — "DoD #5 answered, SPLIT
verdict"**; the evidence is in [docs/chunk5-results.json](docs/chunk5-results.json) and read in
`docs/BASELINE-v3-chunk5.md` §2.

---

## 2026-08-23 — RAG Reliability System: DoD #5 answered, SPLIT verdict — hybrid does not beat vector-only; the abstention half is answered by mechanism, not by a rate

**Decision:** Step 02 DoD #5 ("hybrid measurably beats vector-only") is **closed with a split
verdict**. The answerable half is **NOT MET** on measurement. The abstention half is **ANSWERED
WITH MECHANISM** — the question it was supposed to settle does not exist at
`ABSTENTION_TOP_K = 1`. Both halves rest on one run, recorded figure-for-figure in
[docs/chunk5-results.json](docs/chunk5-results.json) and read in
`docs/BASELINE-v3-chunk5.md`.

**The run was gated before it was believed.** The twelve no-rerank figures were asserted for
exact equality — as `format_report` prints them, no tolerance — against `evaluation.GATE_FIGURES`
before the reranker was constructed. 12/12 PASS, so the corpus and index did not move under the
benchmark. Negatively verified: perturbing one figure by a thousandth aborts at pass 1, names the
arm and both tuples, writes no results file and never loads the model. A mismatch there is
evidence of corpus drift on an unrebuildable corpus, never a threshold to re-bless.

---

### Answerable half — NOT MET

Reranker ON, n = 30, `mrr_depth=10`:

| arm     | recall@5  | recall@10 | recall@30 | mrr@10    |
| ------- | --------- | --------- | --------- | --------- |
| lexical | 0.500     | 0.533     | 0.567     | 0.489     |
| vector  | **0.733** | **0.867** | **1.000** | **0.624** |
| hybrid  | 0.733     | 0.867     | 0.967     | 0.621     |

**Hybrid beats vector-only on none of the four metrics.** It ties on two and loses on two.
**mrr@10 −0.003** is below this benchmark's resolution (one question = 3.3 recall points) and is
recorded as a **tie**, not as "hybrid nearly won".

**The single falsifying number is not a reranked result.** recall@30 is _identical to the
pre-rerank gate tuple in every arm_ — vector 1.000/1.000, hybrid 0.967/0.967, lexical
0.567/0.567. For the single arms it could not have moved: `candidate_k = 30`, so vector's pool is
exactly 30 and reordering inside it cannot change the top-30 set. Hybrid's pool is 40 and was free
to move; it did not. **recall@30 −0.033 is a pool-membership fact that was already true before the
cross-encoder ran:** RRF fuses lexical's 30 with vector's 30, cuts to 40, and drops a gold chunk
vector-only had _out of the candidate pool entirely_. Nothing downstream can recover a chunk the
reranker never receives.

**Consequence — fusion's only measurable net effect on answerable retrieval is that it loses one
gold chunk from the candidate pool.** Everything fusion contributed at depth ≤ 10 was fully
absorbed by the cross-encoder: hybrid and vector are **identical at recall@5 (0.733) and recall@10
(0.867)**. Fusion adds nothing the reranker does not already extract from the vector pool alone,
and it subtracts one chunk.

**Reasoning — what the reranker did to the comparison.** It **converged** the two arms rather than
rescuing fusion. Pre-rerank, hybrid trailed vector by −0.166 recall@5 and −0.255 mrr@10;
post-rerank the two are identical at recall@5 and recall@10 and 0.003 apart at mrr@10. The
cross-encoder repairs the damage RRF did; it does not turn fusion into an advantage. Recorded
flat, including the part that contradicts the stated prediction: reranking made **vector-only
worse** on the shallow metrics (recall@5 0.833 → 0.733, mrr@10 0.700 → 0.624) while lifting
lexical hard (mrr@10 0.142 → 0.489).

### Abstention half — ANSWERED WITH MECHANISM

**The cross-encoder dominates arm choice at rank 1.** Measured, not argued:

- **Pre-rerank candidate pools barely overlap.** Pairwise Jaccard per near-miss, lexical against
  vector: 0.3043, 0.1765, 0.2000, 0.1111, **0.0741**, 0.1765. On u05 that is **four chunks in
  common across a 54-chunk union**.
- **They converge anyway — onto a LURE.** u05 is a near-miss unanswerable, so the chunk tracked
  here is its **lure**, not a gold chunk. It sits at pre-rerank rank **12 (lexical) / 17 (vector)
  / 9 (hybrid)** and the reranker lifts it to **rank 1 in all three**. Observed matched the
  recorded expectation exactly; the driver was built to stop and report rather than adjust it.
- **That makes the claim stronger, not weaker.** The cross-encoder converges every arm onto an
  identical top-1 **regardless of whether that top-1 is a correct answer** — convergence is a
  property of its ranking, not evidence the ranking is right. Since the abstention decision reads
  `hits[0]` and nothing else, the arms are the same system at the point it reads, whatever chunk
  that is. **u05 and u06 are this one phenomenon with opposite outcomes:** u05's lure reaches rank
  1 and the item is SCORED (behaviour exercised); u06's lure does not and the item is UNSCORED
  (behaviour untested).

The arm selects the candidate pool; the reranker decides what surfaces from it. Because the
abstention decision reads `hits[0]` and nothing else, **per-arm abstention comparison is
structurally unavailable at `ABSTENTION_TOP_K = 1`** — with the reranker the arms are the same
system at the point the decision reads, and without it they score near-disjoint item sets
(lexical ∩ vector = one item). This is the 2026-08-23 voidance entry confirmed by direct
measurement of the mechanism behind it.

**Coverage is 5/6, and the missing item is diagnosed, not left open — u06's lure never fired.**
u06 is a near-miss unanswerable, so its `near_miss_to` chunk is the **lure**, not a correct
answer. That lure (p95, the agentic-chunking _pricing_ passage) never reached rank 1: all three
arms rank the p90 chunk describing the _technique_ the question names first. Observed lure ranks —
**lexical: not in pool; vector 2 → 6; hybrid 8 → 6.** Nothing filtered it. At
`ABSTENTION_TOP_K = 1` the decision reads only `hits[0]`, so the lure was never put in front of
it. **u06's abstention behaviour was therefore never exercised — untested, not passed.** That is
precisely what UNSCORED means and why it exists.

**This is not the cross-encoder demotion recorded as the open question below.** That is a
correct-answer demotion on the **answerable** set; this is a lure that failed to fire on the
**unanswerable** set. Pooling the two would feed chunk 7's abstention threshold a lure-miss as if
it were a relevance error, and corrupt the calibration.

**Abstention is therefore reported as ONE collapsed row**, coverage 5/6, scored-set
{u01…u05} identical in every arm, with **no rate** — no abstention decision is wired, and the
`rrf_score` → reranker-score swap stays re-homed to chunk 7's Guardrail threshold. Collapsing is
legitimate only because the three arms scored the same five items; the driver checks that and
refuses to collapse if they ever diverge. u06 is UNSCORED in every configuration.

### OPEN QUESTION carried into chunk 7 — the cross-encoder demotes correct chunks

**Status: OPEN. Not closed by this entry, and not to be read as closed by it.**

**The observation.** Reranking moved **vector-only recall@5 from 0.833 to 0.733**. That is not a
neutral reordering: it means the cross-encoder **demoted a gold chunk out of the top 5 that the
vector arm alone had already ranked inside it**. This is measured on the **answerable** set, where
the chunk in question is a correct answer.

**Scope, stated to prevent a contamination.** u06's UNSCORED status is **not** an instance of this
and must not be cited as one. u06 is a near-miss unanswerable; its `near_miss_to` chunk is a
**lure**, and a lure that fails to reach rank 1 is an untested abstention item, not a demoted
correct answer. Only correct-answer demotions on the answerable set are evidence for this question.

**Why it belongs to chunk 7 specifically.** Chunk 7's Guardrail thresholds **on the reranker's
relevance score** — that is the whole point of re-homing the `rrf_score` → reranker-score swap
there. A cross-encoder that confidently demotes correct chunks is scoring them low, and a
threshold fitted on those scores will inherit the error directly: the system will abstain on
questions it retrieved the right evidence for, and the abstention rate will look like calibration
rather than like a reranker fault. **This bears on abstention calibration, not just on ranking
quality.**

**Recording it flat was correct for chunk 5** — chunk 5's scope is measurement, and the number is
in the table where it belongs. But it must enter chunk 7 as an **open question with a name**, not
as a line in a results table that a future reader scrolls past. Chunk 7 may not fit a Guardrail
threshold on reranker score without first establishing how often the cross-encoder demotes a
correct chunk, and by how much.

### Re-open condition — what would make the abstention half measurable again

This verdict is falsifiable, and these are the two conditions that falsify it:

1. **`ABSTENTION_TOP_K > 1`.** The decision would then read more than the single hit the arms
   agree on, and they may diverge again — at which point a real cross-arm abstention comparison,
   and a real signal choice, come back.
2. **A near-miss set large enough that top-1 convergence is not total.** Six items where all three
   arms agree on all six is not evidence that convergence is universal; it is evidence that it is
   universal _here_. One near-miss on which the arms disagree at rank 1 re-opens the question.

**Changing `ABSTENTION_TOP_K` voids this entry**, exactly as it voids every recorded coverage
figure, because the item set every claim here is fitted on changes. Any entry that changes that
constant must name this entry among the figures it voids, in that entry, at the time it changes it.

**Cross-reference:** rests on **2026-08-23 — "the chunk 5 block is VOID"** and the coverage table
in **2026-08-21 — "near-miss coverage is parameterised, not a property of the golden set"**. The
answerable figures supersede nothing in **`docs/BASELINE-v3-chunk3.md`** — that document's MRR
column (0.707 / 0.450 / 0.152) is the _unbounded_ metric from before `MRR_DEPTH` existed and is a
different key from the `mrr@10` used throughout here.

---

## 2026-08-23 — RAG Reliability System: the chunk 5 block is VOID — the abstention half cannot discriminate arms at `ABSTENTION_TOP_K = 1`

**Decision:** the blocker recorded in **2026-08-21 — "RAG Reliability System: chunk 5 is BLOCKED on
abstention scoring"** is **void**. That entry stands unedited as the record of what was believed;
this one records that its gate no longer holds. Chunk 5's benchmark is not gated on the abstention
signal.

**Reasoning — the measurement that voids it.** At `ABSTENTION_TOP_K = 1` the abstention half of the
benchmark **cannot discriminate between arms in either reranker configuration**, so it cannot
produce a cross-arm result for chunk 5 to gate on:

| configuration      | what the arms do                                                                   | why no comparison exists                                                                              |
| ------------------ | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `--reranker none`  | arms score **different near-miss item sets** (lexical 2/6, vector 2/6, hybrid 4/6) | lexical∩vector = **exactly one item** (u03); rates fitted on different populations are not comparable |
| `--reranker local` | all three arms return an **identical top-1 on all 6 near-misses**                  | the rates are then identical **by construction**, not by measurement                                  |

Either the arms are not comparable or they are trivially equal. There is no configuration at this
`k` in which the abstention half distinguishes them. The underlying per-arm coverage is the table
recorded on 2026-08-21 ("near-miss coverage is parameterised, not a property of the golden set").

**No choice of abstention signal changes this.** Both halves of the table are properties of _which
item is at rank 1_ — set membership without the reranker, set identity with it. A signal computed
**on** that top-1 hit cannot separate arms that were handed the same hit, nor make comparable two
rates fitted on near-disjoint item sets. **This is why moving off `rrf_score` was never the gate:**
the 2026-08-19 finding that `rrf_score` structurally cannot signal abstention remains true and
remains worth acting on, but acting on it would not have unblocked anything, because the blocked
thing does not exist at this `k`.

**This finding is parameterised on `ABSTENTION_TOP_K`, exactly as the coverage figures are.** It is
a statement about `k = 1`, not about abstention. At a higher `k` the arms see more than one hit and
**may diverge again**, at which point a real cross-arm abstention comparison — and a real signal
choice — comes back. **Changing `ABSTENTION_TOP_K` voids this entry the same way it voids every
recorded coverage figure:** the item set every claim here is fitted on changes, so the old and new
statements are not the same measurement. Any entry that changes that constant must name this entry
among the figures it voids, in that entry, at the time it changes it.

**Where the `rrf_score` → reranker-score swap now lives:** **chunk 7's Guardrail threshold**, not a
chunk 5 prerequisite. That is the place the signal is actually consumed as a signal rather than as a
per-arm comparison, and the place where using a rank-position function as a relevance proxy would do
real damage. It is not dropped — it is re-homed, and it moves out of chunk 5's critical path.

**Cross-reference:** the entry this one voids is **2026-08-21 — "RAG Reliability System: chunk 5 is
BLOCKED on abstention scoring"**; its second half was already cleared by **2026-08-21 — "UNSCORED
implemented; half the chunk 5 gate is cleared"**, and the coverage table this entry rests on is
**2026-08-21 — "near-miss coverage is parameterised, not a property of the golden set"**. A reader
arriving at any of those three should read this entry before treating the block as live.

---

## 2026-08-21 — RAG Reliability System: near-miss coverage is parameterised, not a property of the golden set

**Decision:** near-miss coverage is reported **per arm**, and a cross-arm abstention comparison is
suppressed unless the arms scored the same items. Commit `afa0907`, `app/services/evaluation.py`
only.

**The number published in `69f62e7` was mis-scoped.** That commit printed one line — `4/6 SCORED,
2 UNSCORED (u05, u06)` — which reads as a property of golden set v3. It was **hybrid's, and only
hybrid's**: the abstention loop called `retrieve()` with no `arm=` and silently took the `"hybrid"`
default, while the answerable loop directly above it ran all three arms. The other two arms' near-
miss coverage had never been measured. **That reading is withdrawn.**

**Coverage is parameterised on three things**, all three now demonstrated rather than argued.

`ABSTENTION_TOP_K = 1`, `candidate_k=30`, `rrf_k=5`, `rerank_n=40`:

| arm     | `--reranker none` | SCORED             | `--reranker local` | SCORED  |
| ------- | ----------------- | ------------------ | ------------------ | ------- |
| lexical | **2/6**           | u03, u04           | 5/6                | u01–u05 |
| vector  | **2/6**           | u01, u03           | 5/6                | u01–u05 |
| hybrid  | **4/6**           | u01, u02, u03, u04 | 5/6                | u01–u05 |

1. **On `ABSTENTION_TOP_K`** — it defines which hits the check may look at, so it defines the item
   set every rate is fitted on.
2. **On arm** — the table's left half. Three arms, three different scored sets.
3. **On reranker** — the right half. **Reranking collapses the disagreement entirely**: the
   cross-encoder's ordering dominates the top-1 and all three arms converge on an identical set.

**Voiding rule, identical to the `rrf_score` one.** Changing `ABSTENTION_TOP_K` **voids every
abstention figure recorded under the previous value**, exactly as switching the abstention signal
off `rrf_score` does. Neither is a re-baseline that can be argued around: the item set the rate is
fitted on changes, so the old and new numbers are not the same measurement of the same thing. Any
entry that changes that constant must name the figures it voids, in that entry, at the time it
changes it.

**Cross-arm comparisons are invalid at `--reranker none`.** No pair is strictly disjoint — all
three share `u03` — but every pair differs and the overlaps are tiny: lexical∩vector = **1 item**
(u03), lexical∩hybrid = 2, vector∩hybrid = 2, three-way = 1. Comparing two rates each fitted on 2
items that share one question is the v2→v3 error again ("different question populations, not a
before/after on the same questions"). At `--reranker local` the sets are equal and comparison **is**
valid. **Validity is therefore a per-run fact and is now printed per run, never assumed.**

**Chunk 5's headline comparison is the worst case, and no single report can guard it.** Its four
arms are (vector, hybrid) × (rerank, no-rerank), and the question is "does reranking improve
abstention?" — reranked vs not, within one arm: **vector 2 items vs 5** (sharing 2), **hybrid 4 vs
5**. Both on differing sets, and both span two separate `evaluate()` runs, so within-run
suppression is structurally blind to them. The guard is the `scored-set` column: a stable sorted id
list on each arm's row, so two runs' blocks placed side by side show the mismatch on one line.
Deliberately a reporting guard, not persisted state.

**`u06` is never scorable at k=1** — UNSCORED in all six configurations measured. With the
correction already recorded above (its "immune via a15" claim does not hold), **u06 currently
contributes to no abstention figure at all.** Recorded so chunk 5 does not rediscover it as a
surprise mid-run.

**What the report refuses to do.** When the sets differ it prints the shared count and the shared
ids and **nothing else** — no delta, no paired rate over the intersection. At two shared items a
single question moves a rate by 50 points; a paired figure there would be noise wearing a decimal
point. The shared ids are printed so chunk 5 can compute one deliberately and label it, but this
report will not produce one silently.

**Regression gate held.** The twelve answerable figures reproduce byte-identically (lexical
0.233/0.400/0.567/0.142, vector 0.833/0.900/1.000/0.700, hybrid 0.667/0.867/0.967/0.445), the
no-rerank abstention `rrf_score` column is unchanged, and `retrieval.py`'s pre-rerank digest
`3ffa60b3…` still passes. The `--reranker local` run doubles as the **negative control** for the
suppression rule — it is the case that must _not_ suppress, and does not. $0, warm cache.

---

## 2026-08-21 — RAG Reliability System: UNSCORED implemented; half the chunk 5 gate is cleared

**Decision:** abstention scoring now marks a near-miss **UNSCORED** when its `near_miss_to` span
was not retrieved. Commit `69f62e7`, `app/services/evaluation.py` only. This implements what was
recorded on 2026-08-20 (golden set v3, Note 2) and re-stated as the gate on 2026-08-21 — it
invents nothing new.

**Chunk 5 is still blocked.** The gate had two halves. The other one — switching the abstention
signal off `rrf_score`, which structurally cannot signal abstention (2026-08-19) — is untouched
and deliberately so: choosing that signal is chunk 5's decision, not a wiring one, and making it
inside a change scoped to UNSCORED would have settled it by side effect.

**Three outcomes, one of which is live:**

| span retrieved | abstained | outcome                             | counts toward           |
| -------------- | --------- | ----------------------------------- | ----------------------- |
| yes            | yes       | `ABSTAINED` (correct)               | numerator + denominator |
| yes            | no        | `ANSWERED` (confabulation, failure) | denominator only        |
| no             | either    | `UNSCORED`                          | neither                 |

`UNSCORED` depends only on retrieval, so it runs today. The abstain/answer input is
`evaluate(abstained_by=...)`, defaulting to `None` — chunk 5 passes one argument and the other
two outcomes go live through the same code path, not a second one. `None` is kept distinct from
`UNSCORED` on purpose: "retrieval failed" and "nothing decided yet" are the two things this must
never conflate.

**"Retrieved" is the answerable path's rule, reused rather than re-derived.**
`resolve_gold_chunk_ids` already resolved every near-miss's span by containment
(`chunk.char_start <= start AND chunk.char_end >= end`) — it always ran over all 36 entries, not
just the answerable 30 — and the test is chunk-id membership, the same `set(...) & gold` form
`_rank_metrics` uses.

**Evaluated at `ABSTENTION_TOP_K = 1`**, which is what the abstention decision actually consumes:
that `retrieve()` call passes `top_k=1`, and only `hits[0]` is ever read. Not recall@10, not the
`rerank_n=40` pool `retrieve()` builds and then cuts away. Scoring against a set the decision
never saw would credit the abstention to evidence it never had. Named as a constant so the
decision's k and the check's k cannot drift apart.

**Measured, `--reranker none`, hybrid, `candidate_k=30`, `rrf_k=5`: coverage is 4/6.**

| id                 | rank of gold chunk | verdict      |
| ------------------ | ------------------ | ------------ |
| u01, u02, u03, u04 | 1                  | SCORED       |
| u05                | 9                  | **UNSCORED** |
| u06                | 8                  | **UNSCORED** |

**Correction to golden set v3's Note-2 immunity table.** It listed `u04` and `u06` as immune
because their spans are byte-identical to `a22`'s and `a15`'s evidence. `u06` is not immune:
immunity is a property of _a15's question_, not of u06's, and at the k the decision consumes,
u06's gold chunk sits at rank 8. Recorded because that table is otherwise load-bearing for how
chunk 5 reads its numbers — and it is exactly the silent decay the note itself predicted.

**Reporting.** Coverage prints in the same block as the rate, never inferable-only-from-logs, and
always names the UNSCORED ids; a `WARNING` line appears whenever coverage < 6. The rate never
renders as a bare number — it carries its denominator and coverage in the same string
(`3/4 = 0.750 over SCORED items (coverage 4/6)`), and refuses to produce one at all when 0 items
are scored (`n/a (0 scored of 6)`, never `0.000`, which would read as "abstained on nothing"
instead of "measured nothing") or when no decision is wired. `n_scored`/`n_unscored` are report
fields, so a programmatic caller cannot read the rate without them either.

**Nothing recorded is voided.** No abstention _rate_ has ever been recorded — checked all three
places: this log's chunk 3 entry (2026-08-20), `docs/BASELINE-v3-chunk3.md` §3, and SCRATCHPAD.
What exists is a per-question top-1 `rrf_score` column, already caveated in both places as not a
pass rate. This change does not touch that `retrieve()` call, so the column reproduces
byte-identically (u03 0.33333, u01 0.30952, u02 0.26786, u04 0.25000, u05 0.24286, u06 0.21212)
and stays comparable. **Boundary:** when chunk 5 replaces `rrf_score` as the abstention signal,
that column _does_ become non-comparable — and the void belongs to the entry that makes the
switch, not to this one.

**Regression gate held.** `evaluate` with no flags reproduces all twelve answerable figures
exactly (lexical 0.233/0.400/0.567/0.142, vector 0.833/0.900/1.000/0.700, hybrid
0.667/0.867/0.967/0.445), and `retrieval.py`'s pre-rerank digest `3ffa60b3…` still passes.
`git diff --stat` listed one file; `retrieval.py` and the fixture are untouched. $0 — warm cache,
no network.

---

## 2026-08-21 — RAG Reliability System Step 03 Chunk 4: COMPLETE — and the first evidence contradicts the prediction

**Decision:** chunk 4 is closed. The reranker exists, is wired end to end, and all four benchmark
arms are producible. Five commits on `main`, not pushed (no remote):

| Commit    | What                                                                                                       |
| --------- | ---------------------------------------------------------------------------------------------------------- |
| `5db0925` | `app/services/reranking.py` — protocol, `NoOpReranker`, `LocalOnnxReranker`, fetch script, pinned manifest |
| `e806aad` | `retrieve()` accepts a `Reranker`; NoOp proven byte-identical to the pre-rerank path                       |
| `0edbb39` | MRR pinned to a fixed depth (MRR@10); `EvaluationReport` names its reranker                                |
| `797bcde` | ruff pre-commit pin aligned to the venv (v0.8.6 -> v0.16.3)                                                |
| `09598e1` | Reranker constructed from config; **all three arms routed through `retrieve()`**                           |

### The harness gap found at the end, and why it mattered

`evaluate()` isolated its arms by calling `lexical_search()`/`vector_search()` directly, while the
reranker lives in `retrieve()`. `--reranker local` therefore reranked the **hybrid arm alone**.
Chunk 5's "vector + rerank" arm was not producible, and the run would have compared a reranked
hybrid against an un-reranked vector-only and read the difference as a fusion result — confident,
meaningless numbers. Fixed in `09598e1`: every arm goes through `retrieve(arm=...)`. Safe for the
baseline because `_rrf_fuse()` degenerates to the single arm's own order when the other hit list is
empty — asserted, not assumed: with `--reranker none` all twelve recorded figures reproduce exactly.

### Measured — evidence, NOT a verdict

Golden set v3, n=30 answerable, `candidate_k=30`, `rrf_k=5`, `rerank_n=40`, MRR@10. $0 (cached
query vectors). Full reranked run: 3m53s.

| arm     | recall@5      | recall@10 | recall@30 | mrr@10 |     | recall@5     | recall@10 | recall@30 | mrr@10    |
| ------- | ------------- | --------- | --------- | ------ | --- | ------------ | --------- | --------- | --------- |
|         | **no rerank** |           |           |        |     | **+ rerank** |           |           |           |
| lexical | 0.233         | 0.400     | 0.567     | 0.142  | →   | **0.500**    | **0.533** | 0.567     | **0.489** |
| vector  | 0.833         | 0.900     | 1.000     | 0.700  | →   | **0.733**    | **0.867** | 1.000     | **0.624** |
| hybrid  | 0.667         | 0.867     | 0.967     | 0.445  | →   | **0.733**    | 0.867     | 0.967     | **0.621** |

**Reranking DEGRADED vector-only.** recall@5 0.833 -> 0.733, MRR@10 0.700 -> 0.624. The
cross-encoder pushes gold chunks _down_ out of the top 5 that the embedding already had correct.

**This contradicts the stated prediction.** DECISIONS.md, 2026-08-19: _"Step 03's reranker is the
intended fix, since RRF alone can't distinguish lexical signal from lexical noise"_ — the reranker
was expected to recover precision-at-1. It does exactly that for the arms RRF damaged (lexical
+0.347 MRR, hybrid +0.176) and costs accuracy on the arm that was already strongest. Recorded flat,
unsoftened, the same way Step 02's DoD #5 was recorded NOT MET rather than "partially met".

**Second observation:** after reranking, hybrid (0.621) and vector (0.624) sit 0.003 apart —
indistinguishable at n=30, where one question is worth 3.3 recall points. Step 02's DoD #5 may
settle as "no measurable difference" rather than either arm winning.

**Explicitly not a verdict.** Chunk 5 owns it, and cannot draw one yet — see the blocker below.
Nothing above should be read as settling DoD #5.

### Other decisions in this chunk

- **Defaults are deliberately asymmetric.** `Settings.reranker` defaults to `"local"` (the settled
  decision: when a reranker runs, it is the local ONNX one). But `evaluate --reranker` defaults to
  `"none"` and **ignores** `RERANKER` — the benchmark's baseline arm is no-rerank by definition and
  must not move because someone changed an env var. `query --reranker` falls through to config, so
  the flag overrides rather than shadows.
- **MRR pinned to depth 10**, independent of `top_k` — see the separate entry below.
- **Missing/corrupt weights are fatal**, never a fallback to NoOp. Verified in both surfaces: CLI
  exits 4 naming each missing path or the mismatching digest and both hashes; the HTTP app aborts
  in lifespan with `app.state.reranker` never set.

---

## 2026-08-21 — RAG Reliability System: chunk 5 is BLOCKED on abstention scoring

> **VOIDED — see 2026-08-23 — "RAG Reliability System: the chunk 5 block is VOID — the abstention half cannot discriminate arms at `ABSTENTION_TOP_K = 1`".** Annotation only; the entry below is unchanged.

**Decision:** chunk 5 must not run its benchmark until abstention scoring marks an abstention
**UNSCORED** when the `near_miss_to` span was not retrieved.

**Status: specified, NOT implemented.** The requirement was recorded on 2026-08-20 (golden set v3
approval, Note 2) and is still only prose. `evaluate()` today reports, per unanswerable question,
the top-1 `rrf_score` and nothing else — there is no check that the near-miss span was ever in the
retrieved set, and no `UNSCORED` state anywhere in the code.

**Reasoning, restated because it is the reason the block exists:** a near-miss only tests abstention
if the retriever actually surfaced the `near_miss_to` span. If it did not, the system abstained
because retrieval found nothing — a **retrieval failure scored as a correct abstention**. That
inflates the abstention rate and contaminates the Guardrail threshold fitted from it in chunk 7.
`BASELINE-v3-chunk3.md` §3 already carries the warning that no abstention figure recorded so far is
a pass rate.

**Also unresolved and load-bearing for the same run:** DECISIONS.md (2026-08-19) records that
`rrf_score` structurally cannot signal abstention — it is a function of rank position only and never
inspects content. The abstention column is still `rrf_score`. Chunk 5 has to replace it with the
reranker's relevance score, which now exists and, per the table above, is a genuinely different
signal.

**Gate:** implement UNSCORED, and switch the abstention signal off `rrf_score`, before any chunk 5
number is recorded or any DoD #5 verdict is drawn.

---

## 2026-08-21 — RAG Reliability System Step 03 Chunk 4: reranker scores one pair at a time; batching is a correctness bug, not an optimization

**Decision:** `app/services/reranking.py` runs **one (query, passage) pair per
`session.run()`**. No batching, no padding. Committed as `5db0925`.

**Reasoning — measured, not reasoned from first principles.** The plan originally batched all
N candidates into one call and defended determinism with "deterministic batch composition plus
a `round(score, 4)` sort key." Asaad challenged whether padding was actually controlled. It was
not, and the real mechanism turned out to be worse than padding.

`model_quint8_avx2.onnx` is **dynamically** quantized: 50 `DynamicQuantizeLinear` + 50
`MatMulInteger` nodes. Activation scales are computed at runtime from each tensor's min/max
**across the whole batch**. A pair's logit therefore depends on its batch neighbours:

| Probe                                        | Result                                              |
| -------------------------------------------- | --------------------------------------------------- |
| Same pair alone vs. inside a batch of 30     | up to **0.42** logits apart                         |
| Two groupings both padded to a **fixed 512** | still **0.119** apart                               |
| Ranking: solo vs. one batch of 30            | **17 / 435 discordant pairs**, top-10 order differs |
| One batch of 30 vs. two batches of 15        | different top-10 order                              |

**Fixed-length padding does not fix this** — and costs 1.41x throughput (6,408 -> 9,010 ms at
N=30). The `round(..., 4)` key was useless against deltas of ~1e-1, three orders of magnitude
above the rounding.

Batch size 1 is the only invariant configuration, and it wins on every axis at once: bitwise
reproducible across repeated runs and input orderings; **faster** (0.71x of one batch of 40,
because nothing is padded); and bitwise identical across `intra_op` 1/2/4/8, which removes
thread count as a numerics variable entirely. That last point reversed the planned
`intra_op=1` default — it had been justified partly on threads affecting numerics, which is
false, so the default is now **4** (6.9s -> 3.3s at N=40).

**Consequence:** the sort key is exact, `(-score, chunk_id)`, no rounding. The `chunk_id`
tie-break matches `_LEXICAL_SQL`'s existing `score DESC, c.id`. Batching is now the obvious
"optimization" a future reader would apply, so it is guarded three ways: batch-1 by
construction, a `ponytail:` comment naming the trap, and a **negative control** in `_demo()`
that requires batched scoring to still differ (currently 0.358 logits). A test that can only
pass proves nothing — the same discipline chunk 2 used when it injected six defects into
`verify_golden_set.py`.

**Also decided in this chunk:**

- **Fan-out N = 40, not 30.** Reranking reorders but never recovers, so recall@N is a hard
  ceiling. Measured on golden set v3: vector-only recall@30 = **1.000**, hybrid = **0.967**.
  The single shortfall is `a13`, whose gold chunk sits at **rank 35 of a 52-deep fused pool** —
  a truncation artifact, not a retrieval failure. hybrid@40 = 1.000. At N=40 both arms enter
  chunk 5 at a 1.000 ceiling, so any chunk-5 miss is unambiguously the reranker. Table in the
  Step 03 note. K stays 5 so chunk 5 stays comparable to chunk 3.
- **`NoOpReranker` is first-class**, not test-only. Chunk 5's no-rerank arm must travel the
  identical code path; an `if reranker is not None` branch would be exactly the second path
  that requirement forbids.
- **Failure policy: the library raises, the boundary decides.** `RerankError` is not caught by
  `retrieve()`. The benchmark hard-fails for free (a row labelled "reranked" that silently
  isn't is what `GoldenSetError` exists to prevent); the serving layer catches and degrades to
  retrieval order, recorded — never silent. Missing/corrupt weights are **fatal at startup**,
  like the `document_status` enum drift check and unlike the Postgres/Chroma probes: a missing
  baked-in weight is a build defect that will never fix itself.
- **`onnxruntime==1.29.0` pinned exactly**, with `tokenizers` and `numpy` promoted to explicit
  deps. All three were already installed transitively via chromadb — `uv sync` reports no
  changes — but the negative control asserts a property of ORT's dynamic quantization, so an
  unpinned runtime could break it for reasons unrelated to this code.
- **Pinned model revision** `233902d25c440f23af6f7d6e94d2946bac0bee0a`, recorded in two
  independent places: the SHA in `scripts/fetch_reranker_model.py` (URLs are
  `/resolve/<sha>/`, so a force-push cannot change what arrives) and per-file checksums in
  `scripts/reranker_model.sha256` (so a compromised CDN cannot either). Verified at build and
  at startup; one corrupted byte makes `--verify` exit 1, failing the Docker build.
- **`quint8_avx2` over the AVX-512 variants:** the repo ships no generic int8 file, and this
  machine (i7-8665U) has AVX2 and no AVX-512. Unsigned activations are ORT's recommendation on
  x86 without VNNI.

**Image cost:** +22.8 MiB of weights (23,200,716 + 711,396 + 794 bytes) and **zero new
wheels**. The fp32 ONNX at the same revision is 91 MB, so int8 saves 64.7 MiB.

**Latency, recorded flat:** ~3.3 s for N=40 at `intra_op=4` on the development machine. Fine for the chunk
5 benchmark, questionable for serving — Step 04 may need a smaller serving N. Not solved here.

**Not yet wired.** `retrieve()`, `evaluation.py`, `cli.py`, `main.py`, and `config.py` are
untouched; the component exists and self-checks but nothing calls it.

---

## 2026-08-21 — API Contract 1.1.0 amendment: APPROVED, NOT YET APPLIED

**Status: pending.** Asaad approved the amendment on 2026-08-21; `API Contract.md` still reads
1.0.0 and must be edited when the reranker is wired into the response path.

**Decision:** add to §5.1 — `timings_ms.rerank`, and a top-level `degraded: []` array — plus a
`rerank_degraded` row in §8's mock scenarios. Version **1.0.0 -> 1.1.0**.

**Reasoning:** verified against the contract's own test rather than asserted. The header rule
is _"Changing a field name, status enum, or error code is a breaking change."_ §5.1's body is
exactly `id`, `conversation_id`, `role`, `content`, `status`, `citations`, `trust`,
`guardrail`, `plan`, `timings_ms`, `created_at`.

| Field               | Present in §5.1? | Change type                                   | Version  |
| ------------------- | ---------------- | --------------------------------------------- | -------- |
| `citations[].score` | **yes** (`0.82`) | filling it from the reranker alters no schema | **none** |
| `timings_ms.rerank` | no               | additive key                                  | 1.1.0    |
| `degraded`          | no               | additive field, default `[]`                  | 1.1.0    |

Neither addition renames a field, touches an enum, or changes an error code — additive and
backward compatible, so a minor bump, one covering both. `citations[].score` needs no bump at
all: this log (2026-08-19) and the contract already assign it to the reranker.

`timings_ms.rerank` is not optional bookkeeping — Step 03's quality gate requires recording
the p50/p95 latency cost of reliability, and folding rerank into `retrieve` hides exactly that
number. `degraded` exists because silent degradation is unacceptable in this system: when the
reranker fails mid-request the answer still ships, in retrieval order, and the response has to
say so. §8's scenario row is required by the contract's own rule that every UI state be
reachable from the mock layer.

**Binding constraint on Step 04, unresolved:** when reranking is off or degraded, the contract
still wants a `citations[].score`. It must **not** be backfilled from `rrf_score` — the
2026-08-19 entry records that `rrf_score` is not a calibrated relevance number and structurally
cannot signal abstention. Step 04 decides what goes there; omitting it is the safe default.

## 2026-08-20 — RAG Reliability System Step 03 Chunk 3: re-baseline on v3; v2 reproduces exactly; hybrid trails vector-only on v3

**Regression check passed first, before anything was interpreted.** Three retrieval-path files had
changed since the Step 02 run (all in the CLI chunks): `retrieval.py` gained `rrf_k` and `arm`
parameters, both defaulting to prior behaviour; `embeddings.py` gained two log lines on retry
paths; `vector_store.py` gained a read-only `count_by_document`. Rather than assert these were
inert, the v2 fixture was re-run: **all eleven recorded figures reproduce exactly** (lexical
0.409/0.727/0.479, vector 0.727/0.818/0.690, hybrid 0.818/0.818/0.633), abstention included
(q09 0.24359, q10 0.29167 vs recorded 0.244/0.292). $0 — no `allow_network`, so a cache miss would
have raised. This also confirms end to end that offset-resolved gold sets are identical to v2's
stored `chunk_ids`.

**v3 result, answerable only, n=30, no reranker, rrf_k=5, candidate_k=30:**

| arm              | recall@5  | recall@10 | MRR       |
| ---------------- | --------- | --------- | --------- |
| lexical-only     | 0.233     | 0.400     | 0.152     |
| vector-only      | **0.833** | **0.900** | **0.707** |
| hybrid (RRF k=5) | 0.667     | 0.867     | 0.450     |

On the same questions, hybrid trails vector-only by 0.167 recall@5, 0.033 recall@10, 0.257 MRR.
At n=30 one question is 3.3 recall points, against 9.1 at n=11.

**v2 -> v3 delta.** lexical: -0.176 / -0.327 / -0.327. vector: +0.106 / +0.082 / +0.017. hybrid:
-0.152 / +0.048 / -0.182. The two sets are **different question populations**, not a before/after
on the same questions — no entry is shared. The delta measures how far each arm's standing depends
on which benchmark it is measured against, not a change in retrieval behaviour.

**Abstention, 6 near-misses, scored separately and never folded into recall/MRR:** top-1 rrf_score
0.212-0.333 (u06 0.212, u05 0.243, u04 0.250, u02 0.268, u01 0.310, u03 0.333) — inside the same
band the v2 unanswerables occupied. **These are not a pass rate:** the requirement recorded above
(2026-08-20, note 2) that an abstention counts only when the `near_miss_to` span was actually
retrieved is chunk 5's and is not implemented.

**Hypothesis, explicitly not a finding:** the lexical arm falls hardest (recall@10 and MRR both
-0.327). v3's questions were authored to avoid the passage's vocabulary, measured with the same
stemmer the lexical arm uses; v2's were not. A lexical arm doing worse on questions written not to
share words with their evidence is the expected direction, but this run does not isolate that from
the change in n, in question population, or in evidence spans.

**No DoD #5 verdict is drawn here.** That is chunk 5's, after the reranker exists. Recorded flat.

**Cost:** v2 arm $0 (13/13 cached). v3 arm one Voyage request, 36 questions, 3,366 chars, ~841
tokens, **~$0.0000168 actual spend**. Cache 13 -> 49 entries; further reruns of either fixture are
$0. Corpus untouched — no re-embed, 260 vectors before and after.

**Commits:** `7db5f3f` (offset-resolved gold ids) and `7b3c876` (the run). Full tables in
[docs/BASELINE-v3-chunk3.md](docs/BASELINE-v3-chunk3.md).

---

## 2026-08-20 — RAG Reliability System: golden set v3 APPROVED

**Decision:** golden set v3 is approved by Asaad and may now be measured against.
`tests/fixtures/golden_set_v3.json` — 36 entries, 30 answerable + 6 near-miss, exactly 3
answerable per page-decile across the 247-page corpus, no empty regions. v2 is retained
untouched so chunk 3 can compare. `scripts/verify_golden_set.py` passes: every offset resolves
through its containing chunk and every snippet matches byte for byte.

**Anchoring:** canonical-text offsets resolved _through the containing chunk_, and **no
`chunk_ids` stored**. v2's offsets were fine; the problem was that `evaluation.py` scored recall
on `chunk_ids`, which are `chk_` ULIDs minted fresh on every ingest — one reindex silently zeroed
the gold set. v3 resolves gold chunk ids from offsets at evaluation time instead, which requires a
small `evaluation.py` change in chunk 3.

**No `status_changed_at`-style shortcut on verification either:** the source PDF is gone and the
canonical text cannot be reconstructed (246 of 259 consecutive chunk pairs have gaps), so
"resolves in the live document" was redefined as "resolves through the owning chunk". Exact,
because `chunking.py` guarantees `canonical_text[cs:ce] == chunk.text`.

**Verification was itself verified.** The script passing on a clean file proves nothing, so six
defects were injected — offset shifted by one, unknown `document_id`, an answerable entry carrying
near-miss fields, an unanswerable entry missing `why_unanswerable`, a span widened across chunk
gaps, and a duplicate id. All six were caught, producing eight messages, all printed, exit 1.

**Anti-leakage authoring.** Questions were written from the information need rather than by
paraphrasing the passage, and overlap was measured with Postgres `to_tsvector('english', ...)` —
the same stemmer the lexical arm uses, so the number measures what actually inflates BM25.
Distribution: mean 0.157, median 0.167, min 0.00, max 0.43; 5 questions share no lexeme at all
with their snippet. Five entries breached 0.30 and were resolved during review, not merely
displayed (two were later dropped in the decile rebalance). One remains above the line, `u01` at
3/7 = 0.43, deliberately: all three shared lexemes are the product name, and a question about one
named model's price cannot avoid naming it. Padding it to dilute the ratio would game the metric
without reducing leakage.

**Near-miss discipline.** Five of six probe sets initially returned non-zero rows. Every matching
chunk was read rather than tightening probes until they reported zero; all were the probe matching
a _different subject_ (embedding-API pricing, agentic chunking's $0.001/call). Non-zero probes are
stored with row counts and inspection notes, and the generator hard-fails on a non-zero probe
lacking one.

### Note 1 — known property of the overlap metric, not a defect

Several questions use meta-phrasing ("what does the book say", "which does the book name"). Those
lexemes intersect nothing in the snippet, inflating the denominator and deflating the ratio. The
mean of 0.157 is therefore **optimistic**, and not directly comparable to a set phrased without
meta-references. Read it as an upper bound on cleanliness, not a measurement of it.

### Note 2 — design requirement carried into chunk 5

A near-miss only tests abstention if the retriever actually surfaced the `near_miss_to` span. If
it did not, the system abstained because retrieval found nothing — a retrieval failure scored as a
correct abstention. That would confound the abstention rate and contaminate the Guardrail
threshold fit downstream.

**Requirement:** chunk 5's benchmark must report, per unanswerable question, whether the
`near_miss_to` span was present in the retrieved set. An abstention where it was **not** is
**UNSCORED, not a pass**.

Two entries are already immune, because their `near_miss_to` span is byte-identical to an
answerable entry's evidence span, so retrieval is separately scored on exactly that span
(verified against the fixture, not assumed):

| near-miss                      | shares its span with                                 | status                        |
| ------------------------------ | ---------------------------------------------------- | ----------------------------- |
| `u04` (MTEB update cadence)    | `a22` (58 datasets), offsets 149202-149301           | immune                        |
| `u06` (context-enriched price) | `a15` (agentic chunking cost), offsets 106304-106470 | immune                        |
| `u01`, `u02`, `u03`, `u05`     | no answerable entry covers their span                | **needs the retrieval check** |

Note `u03` was immune in an earlier draft via `a16`, which the decile rebalance dropped. That is
exactly how this guarantee decays silently — the check belongs in the benchmark, not in the
fixture's shape.

**Not yet done:** nothing has been measured against v3. Chunk 3 needs ~36 query embeddings — one
Voyage request, ~900 tokens, ~$0.00002, inside the free tier — and that call requires its own
approval.

---

## 2026-08-20 — RAG Reliability System: status transitions enforced in SQL, not Python

**Decision:** `app/documents/status.py` is now the only place in the codebase that writes
`documents.status` on an existing row. `transition()` issues one conditional
`UPDATE ... WHERE status IN (legal predecessors) ... RETURNING`; the legality check _is_ the WHERE
clause. The disambiguating `SELECT` (gone vs. illegal) runs only when zero rows come back, so the
happy path stays one round trip. `LEGAL_PREDECESSORS` is inverted from `LEGAL_TRANSITIONS` at import
time, never hand-written twice. Verified live: all 36 (from, to) pairs behave per the table (10 legal,
26 illegal), and two concurrent sessions attempting the same transition produce exactly one winner.

**Reasoning:** a Python-side `if doc.status in ...` check followed by a write is a read-then-write
race — two workers both pass the check and the second clobbers the first. Pushing the predicate into
the UPDATE makes Postgres READ COMMITTED do the work: the loser blocks on the winner's row lock,
re-evaluates its WHERE against the committed row, matches nothing, and raises. No application lock,
no retry loop, no advisory lock.

**Rejected — `documents.status_changed_at`:** it would be a hand-synced denormalisation of
`MAX(created_at)` over `audit_events`, which already records every transition. The one thing it buys
that the audit table does not is a cheap "stuck in parsing > 10 min" sweeper query. No sweeper exists.
Add the column when one is written. No migration was created for this chunk.

**Rejected — `from` in the audit payload:** Postgres 16's RETURNING sees only the new row, and reading
the old one first reintroduces exactly the read-then-write being eliminated. The predecessor is
recoverable from the preceding audit row; the upgrade path, if ever needed, is a CTE capturing the old
value in the same statement.

**Three behaviour changes this forced, all consequences of the transition table, all deliberate:**

1. `quarantined` is now genuinely terminal. Re-ingesting a quarantined document used to retry it and
   now returns it untouched — quarantine is a final answer about a document's structure.
2. `reindex` no longer hand-sets `status = "queued"` (an illegal move under the table). The CLI stops
   driving the state machine entirely and passes `force=True`, so a `ready` document takes the
   `ready -> parsing` edge directly.
3. A document stranded in `parsing`/`indexing` by a crashed run used to be forced silently back to
   `queued`. The table has no such edge, so recovery is now the explicit route it _does_ contain —
   sweep to `failed`, then retry from `queued` — which leaves the abandonment in the audit trail
   instead of erasing it. The table needed no widening; it was already self-consistent.

**Also:** `create_type=False` on the SQLAlchemy enum (the pg type is created by migration 0002's own
inline literal; leaving it `True` makes autogenerate emit a redundant `CREATE TYPE`), and a fatal
startup assertion comparing `DocumentStatus` against the live `pg_enum` labels as an _ordered_ tuple —
fatal unlike the neighbouring reachability probes, because a drifted schema is a correctness problem,
not an availability one. Gated on Postgres being reachable so an unreachable DB still yields a live
app that can report 503.

**Tests:** `_demo()` self-check, not pytest — `tests/` holds only fixtures, every check in this repo
is an in-module `_demo()`, and installing pytest is an approval gate. Step 06 introduces a real suite
for the whole repo; adding it here for one module would fragment the convention for nicer failure
output, not for correctness.

---

## 2026-08-20 — RAG Reliability System: Step 02 closed with DoD #5 open, not silently passed

**Decision:** Step 02 (Pipeline Core) is closed. Five of six Definition-of-Done items are met,
re-verified live through the CLI in Chunk 6d: full ingest (260/260 chunks/vectors matching), a
genuine $0 dedupe no-op on re-ingest (proven live, not just at the unit level — no `--allow-network`
flag, no HTTP request logged, chunk/vector counts unchanged before and after), provenance-carrying
`query` results, all seven documented error cases (corrupt/encrypted/empty/no-text-layer/oversized/
429/5xx) producing the right status and a clean exit code with zero tracebacks, and orphan-free
deletion. DoD #5 — hybrid retrieval measurably beating vector-only — is recorded as **NOT MET**,
provisional: hybrid wins recall@5 and ties recall@10 but loses on MRR to vector-only at every RRF k
swept 1-100 (see the 2026-08-19 Chunk 5 entry below for full numbers). This is deliberately not
softened to "partially met."

**Reasoning:** the root cause is architectural (RRF cannot separate lexical signal from the noise
introduced by the `websearch_to_tsquery` OR-rewrite), not a tuning gap this step can close — fixing
it requires the reranker, which is Step 03's job. Hybrid still ships as the CLI/Step 03 default
regardless (2026-08-20 entry above), on the recall-is-unrecoverable-downstream argument, not because
this DoD item cleared. Recording it as open, with the reasoning, is the point: a future session must
not assume Step 02 fully validated the retrieval strategy — it validated everything except that one
measurable claim.

**Also carried forward, not silently dropped:** the context budgeter (pack retrieved chunks into a
token budget, report what's dropped) was scoped into this step's task list but never built in any
chunk. It blocks nothing in this step's DoD, but Step 03 or later must build it before an LLM call
receives unbounded context.

---

## 2026-08-20 — RAG Reliability System: path allowlist is CLI-only; `ingest_document` accepts arbitrary paths by design

**Decision:** the Chunk 6 path-allowlist check (Quality Gates' "parsing runs on paths inside a
controlled directory only") is enforced **only at the CLI boundary** (`app/cli.py`, `ingest`/
`reindex --file`), not inside `app/services/ingestion.py`'s `ingest_document()`. The service
function accepts and parses whatever `Path` it's given — no allowlist, no containment check —
by design, not by oversight.

**Reasoning:** `ingest_document()` is a general-purpose library function two different trust
boundaries will call. The CLI's boundary is "a local operator typed a path" — that's what the
allowlist is for. Step 04's HTTP layer is a different boundary entirely: a multipart upload gets
written to a server-controlled temp location by the HTTP layer itself, and _that_ server-chosen
path is what should reach `ingest_document()` — never a client-supplied path string. Baking a
CLI-shaped "is this under an allowed root" check into the service layer would conflate those two
boundaries and give Step 04 a false sense that path safety is already handled below it.

**Binding constraint for Step 04:** the HTTP layer must never pass a user/client-controlled path
directly to `ingest_document()`. It must stage the upload itself (e.g. a server-generated temp
path) and pass that. This is a requirement on Step 04's design, not just a note — flagging it here
so it isn't rediscovered as a vulnerability later.

**`ingest_root` resolution:** the allowlist root (`Settings.ingest_root`) is resolved to an
absolute, symlink-canonical path at config-load time, anchored to the repo root via
`Path(__file__).resolve().parents[2]` in `app/core/config.py` — not to the process's working
directory. A relative default resolved against CWD would let the security boundary itself silently
move if the CLI is ever launched from outside the repo; anchoring to the source file's own location
makes the default correct on any clone regardless of invocation directory, while an explicit
absolute `INGEST_ROOT` override (e.g. a container volume mount) still passes through untouched
apart from symlink canonicalization. Verified: identical resolved path when `get_settings()` runs
from the repo root vs. from `/`.

---

## 2026-08-20 — RAG Reliability System: hybrid is the default retrieval arm (CLI + Step 03)

**Decision:** `hybrid` (RRF, k=5) is the default retrieval arm for the CLI's `query` command and for
Step 03's Retriever agent. `lexical` and `vector` remain fully selectable — only the default is
fixed.

**Rationale:** a reranker (Step 03) can reorder candidates it receives but cannot recover a document
that was never retrieved in the first place — a retrieval-stage false negative is unrecoverable
downstream, while a bad rank order is exactly what reranking exists to fix. Recall is therefore
prioritized at the retrieval stage; ordering is deliberately left to be repaired downstream, not
optimized for here.

**Explicitly not based on the measurement:** at n=11, hybrid's recall@5 edge over vector-only (0.818
vs 0.727) is a single question crossing into the top 5, not a trend, and hybrid loses on MRR to
vector-only at every RRF k swept 1-100 (0.633 best case vs 0.690). The default rests on the
architectural argument above, not on this measurement clearing a bar — if the argument didn't hold,
this measurement alone would not justify defaulting to hybrid.

**Both arms stay selectable** — via the CLI's `--arm` flag now, and whatever Step 03's Retriever
exposes later — so the default can be revisited once the reranker is measured end-to-end without
touching retrieval code.

---

## 2026-08-19 — RAG Reliability System Step 02 Chunk 5: hybrid retrieval fused with RRF, k tuned to 5, MRR regression documented not fixed

> **PARTIALLY SUPERSEDED — see 2026-08-31 — "Step 03 Chunk 7.2: exact brute-force vector search replaces Chroma/HNSW; six carried claims corrected".** The vector arm's *backend* changed (Chroma/HNSW -> exact brute-force search over a tracked artifact); RRF itself, `RRF_K = 5`, the dedupe-by-chunk_id rule and the recorded MRR regression are **unchanged and still stand** — all 12 gate figures reproduce exactly under the new backend. Annotation only; the entry below is unchanged.

**Decision:** `app/services/retrieval.py` fuses Postgres FTS (`content_tsv` + `ts_rank_cd`) and
Chroma vector search by Reciprocal Rank Fusion, deduped by `chunk_id` only (never by span overlap
— the chunker's 300-char overlap between adjacent chunks means a span-overlap rule would drop the
neighbour q11-q13 in the golden set need retrieved alongside it). `RRF_K = 5`, chosen by sweeping
k=1..100 against the golden set (offline, cached query vectors, $0) rather than the SIGIR-2009/
Elasticsearch default of 60. `websearch_to_tsquery`'s default AND-of-lexemes returns 0 rows for
10/11 answerable golden questions on this corpus; rewriting the top-level `&` to `|` (still via
`websearch_to_tsquery`, so stemming/phrases/negation survive) was necessary to make the lexical arm
contribute anything, taking lexical-only recall@5 from 1/11 to 6/11.
**Measured result — DoD #5 partially met:** hybrid (k=5) beats vector-only on recall@5 (0.818 vs
0.727) and ties recall@10 (0.818), but **loses on MRR** (0.633 vs 0.690) at every k tested. Cause:
the OR-relaxation needed to make lexical non-empty also makes it noisy, and RRF at any k lets that
noise occasionally outrank a chunk vector search already had correct at rank 1 (4/11 questions
regressed this way, 3/11 improved, net negative). Not fixed in this chunk — recorded as a known
limitation; Step 03's reranker is the intended fix, since RRF alone can't distinguish lexical signal
from lexical noise. Full numbers in the vault's Step 02 planning notes (not mirrored here).
**Consequence for Step 03:** raw `rrf_score` does not separate answerable from unanswerable queries
at k=5 (unanswerable top-1 scores 0.244/0.292 fall inside the answerable range 0.214-0.333) — it was
never meant to be a calibrated relevance number (API Contract §5.1's citation `score` is filled from
the reranker in Step 03, not from fusion). The groundedness/abstention threshold must come from the
reranker, not from `rrf_score`.
**Cost:** query-embedding call for the golden set — 13 questions, 330 tokens (char/4 estimate), 1
Voyage request, $0.0000066 actual spend. Cached to `tests/fixtures/query_embeddings.json`
(gitignored, derived data); every subsequent evaluation run is $0 and offline.
**Amendment (same day) — sample size and provisionality:** all recall/MRR numbers above are over
**n=11** answerable questions, so **one question = 9.1 recall points**. The recall@5 "win" (0.727 →
0.818, i.e. 8/11 → 9/11) is a **single question** crossing into the top 5, not a trend — at this n
it is not a basis for further fusion tuning, which is why the k sweep stopped at documenting k=5
rather than continuing to chase MRR. Treat every number in this entry and in the Step 02 note as
**provisional**: `golden_set.json` v2 has open review items — chunk-ordinal anchoring (whether
`chunk_ids` in `evidence` still match current chunker output after any future re-chunk) and
whether q09/q10 are genuinely out-of-domain absences or near-miss unanswerables (content adjacent
enough to a real passage that a retriever could plausibly surface a false-positive top hit) — that
have not yet been resolved. Numbers should be re-pulled once those review items close, not treated
as a stable baseline to regress against.
**Amendment (same day) — why `rrf_score` structurally can't signal abstention:** RRF's score is a
function of rank position only (`1/(RRF_K+rank)`) — it never inspects content, so it cannot be low
"because the answer isn't there." As long as either arm returns at least one candidate — and the
lexical arm's OR-relaxation (line above) all but guarantees that for any query sharing a single word
with the corpus — a top-1 result exists with a top-1-shaped score whether or not that chunk is
actually relevant. The empirical overlap already recorded above (unanswerable top-1 scores
0.244/0.292 sitting inside the answerable range 0.214-0.333) is the direct consequence of this, not
a k-tuning artifact — no k fixes it, because the score was never measuring relevance to begin with.
Abstention therefore needs an **independent groundedness signal in Step 03** (the reranker's
relevance score, or the Verifier's claim-support score) — not a threshold on any retrieval-stage
score, RRF or otherwise.
**Amendment (same day) — why k=5 shipped over the conventional k=60:** the sweep's recall@5 win
(0.727→0.818) holds flat across k=1-6, then drops back to vector-only's 0.727 at k≥7 — so k=5 is
inside the plateau, not a cherry-picked single point. Within that plateau, k=2 and k=3 scored
marginally higher on MRR (0.637 vs k=5's 0.633) but the gap (0.004) is a fraction of what one
question's rank shift is worth at n=11 (1/11 ≈ 0.091) — noise, not a signal worth chasing per this
session's instruction not to tune fusion weights on 11 questions. k=5 was picked as the **largest,
most conservative k inside the winning plateau**: it captures the same recall@5 gain as k=1-4 while
staying furthest from the extreme low-k end (k=1-3), where fusion is most sensitive to exact
top-rank swings and therefore most prone to overfitting a small golden set. It remains far below the
literature/Elasticsearch default of 60, which this corpus measurably loses to vector-only under
(recall@5 0.727, MRR 0.612) — 60 is tuned for corpora where lexical is a strong, low-noise signal,
which the OR-relaxed arm here is not.
**Clarification (2026-08-20) — how the sweep was actually run:** `_rrf_fuse()`/`retrieve()` took no
`rrf_k` parameter at the time of this sweep — that parameter was added later, in Chunk 6b, for the
CLI's `--rrf-k` flag. The sweep itself was a throwaway, uncommitted script that pre-fetched each
question's raw lexical/vector hit lists once, then looped `k in [1..100]` reassigning the module
attribute directly (`retrieval_mod.RRF_K = k`) before each `_rrf_fuse(lex, vec)` call, since
`_rrf_fuse` at the time read `RRF_K` from module scope internally on every call. The numbers stand:
each iteration's mutation took effect before that iteration's fuse call, so every k was computed
correctly against the same cached hit lists. But the method was module-global mutation in a scratch
script, not a clean parameter override — recording this so the method matches what's on the record,
not what Chunk 6b's later refactor might imply in hindsight.

---

## 2026-08-19 — RAG Reliability System Step 02 Chunk 4: voyage-4-lite embeddings live, tokenizer dependency skipped

**Decision:** Embedding backend is Voyage `voyage-4-lite`, 1024 dimensions, wired via `VoyageEmbedder`
(batching + bounded retries + timeout) behind an `Embedder` protocol, with a deterministic
`FakeEmbedder` for $0 pipeline testing. The real embedding run completed on the 247-page corpus:
260 chunks, `document_id=doc_01M0D39WZDYY7STA3PHWT5R4C7`, status `ready`, persisted in Postgres and
the `chunks_voyage4lite_1024` Chroma collection (260/260 vectors verified). Declined adding the
`tokenizers` package (HuggingFace's Rust tokenizer bindings) to get an exact, measured token count
for the pre-run cost estimate; proceeded with the char/4 estimate (68,273 tokens) instead — a
deliberate skip, not an oversight.
**Reasoning:** At $0.02/1M tokens, the gap between a char/4 estimate and an exact tokenizer count is
a rounding error, not worth a new dependency for a one-time cost estimate — especially since the
actual spend lands inside Voyage's 200M-token free tier either way (measured or not). `voyage-4-lite`
was chosen for its free-tier size and native `input_type=query/document` asymmetric embedding
support, consistent with the project's Claude-based generation stack.

---

## 2026-08-19 — RAG Reliability System Step 01: uv, explicit pool sizing, config-only LLM check

**Decision:** Repo scaffolded at the repo root. **uv** over
poetry as dependency manager — already installed, so no new global tooling and no
approval gate; PEP 621 `pyproject.toml` + `uv.lock`, app declared `package = false`.
Stack pinned: Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic (async env,
`sqlalchemy.url` blank in the tracked `.ini`, injected from Settings), ChromaDB 1.5.9
persistent client behind a `VectorStore` Protocol with `anonymized_telemetry=False`.
Quality floor: ruff + mypy strict + pre-commit + system gitleaks (local hook — the
upstream hook needs a Go toolchain).
**Pool sizing:** `pool_size=10`, `max_overflow=5`, `pool_pre_ping=True`,
`pool_timeout=30` — explicit, not SQLAlchemy's defaults (5/10, no pre-ping). Ceiling
is 15 connections per process → **~6 uvicorn workers** against `max_connections=100`
(97 usable after the superuser reserve). RAG-specific: holding a DB session across an
LLM call caps throughput near **20 req/s** at 4 workers regardless of hardware — the
constraint is recorded in Step 04's Notes.
**`/health/ready` `llm` check is config-presence only** (key non-empty, no request) —
paid APIs are gated. Keeps API Contract §6's three-key shape; Step 02 replaces it with
a real probe.
**Reasoning:** Every later step inherits this floor, so secrets handling, error shape
and capacity limits are decided now rather than retrofitted. uv because the cheapest
tool that works is the one already on the machine.

---
