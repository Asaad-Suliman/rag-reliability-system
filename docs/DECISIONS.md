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
