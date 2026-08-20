# Step 03 · Chunk 2 — Golden set v3

**Status:** D1-D5 approved 2026-08-20. Drafting under way. Nothing measured against v3.
**Cost so far:** $0. Every finding below came from Postgres reads and local file reads.

---

## 0. Blocking finding — "resolves in the live document" has no referent

The spec says verification must prove each `(char_start, char_end)` *"resolves in the live document
and the extracted text equals snippet exactly."* **There is no live document text to resolve
against.** Three independent confirmations:

1. **The source PDF is gone.** `data/uploads/` — the configured `ingest_root` — is empty. The
   corpus was ingested 2026-08-19 and the file has since been removed.
2. **Canonical text is not persisted.** `documents` has no text column; only `chunks.text` survives.
3. **It cannot be rebuilt from the chunks.** `chunking.py` defines offsets as absolute positions
   into the *canonical text* (`PAGE_SEPARATOR.join(page.text for page in pages)`), but the 260
   chunks do not tile it. Measured on the live table:

   | consecutive chunk pairs | count |
   |---|---|
   | **gap** (`char_start > prev char_end`) | **246** |
   | overlap | 12 |
   | exact join | 1 |

   The chunker discards material between chunks. 246 gaps means the concatenation of chunk texts is
   not the document, and no stitching recovers what was dropped.

### Proposed resolution — anchor to canonical offsets, resolve *through the owning chunk*

Keep exactly what the spec asks for: **document-level canonical offsets, not chunk ordinals.** Change
only how verification resolves them. For a span contained in one chunk:

```
extracted = chunk.text[entry.char_start - chunk.char_start : entry.char_end - chunk.char_start]
```

This is exact, because `chunking.py:191` asserts `canonical_text[cs:ce] == chunk.text` — the chunk
text *is* the canonical slice. **Verified working before proposing it**, against v2's q01:

```
$ SELECT ordinal, char_start, char_end,
         substr(text, 5479 - char_start + 1, 5509 - 5479)
  FROM chunks WHERE char_start <= 5479 AND char_end >= 5509;

  7 | 5036 | 6520 | 1 million token context window     <- matches v2's recorded quote exactly
```

The offsets stay document-level and stay meaningful if the document is ever re-parsed. The chunk is
the lookup vehicle, not the anchor.

**Constraint this imposes:** every `snippet` must fall inside a single chunk. Chunks run 69–1999
chars (mean 1052), so this only rules out snippets spanning a gap. Not a real limit for short
evidence spans, but the verification script will fail loudly rather than silently if one does.

**Decision D1 — needs your yes.** If you want offsets verifiable against a true canonical text
instead, that requires re-obtaining the PDF and re-parsing it. Parsing is local and $0, but the file
does not currently exist on disk. Say so and I will stop and ask you for it.

---

## 1. Two corrections to the brief

Both matter because they change what v3 has to fix.

### 1.1 v2 did not anchor on ordinals

The brief says *"v2 used ordinals and they are unstable."* v2's evidence blocks actually carry
document-level `char_start` / `char_end` / `quote` / `page` — the same anchoring style v3 asks for.
The word "ordinal" appears twice in the whole file, both times inside prose `why_included` strings,
never as an anchor.

**The real instability is `chunk_ids`, and it is worse than ordinals.** `evaluation.py:165` scores
recall on them:

```python
gold = {cid for ev in q["evidence"] for cid in ev["chunk_ids"]}
```

`chunk_ids` are `chk_` ULIDs minted fresh on every ingest (`ingestion.py`: `Chunk(id=new_id("chk"))`).
A reindex — which chunk 1 just turned into a first-class supported operation — regenerates all 260
and silently zeroes every gold set. The offsets in v2 are correct and stable; they are simply *not
what the metric reads*.

**So v3 must not store `chunk_ids` at all.** Gold chunk ids get resolved at evaluation time from the
offsets, via the containing-chunk lookup above. That keeps v3 valid across any number of reindexes.

**Dependency this creates:** `evaluation.py` needs a small change to resolve gold ids from offsets
instead of reading them from the file. That change belongs to **chunk 3**, not this one. Naming it
now so it is not a surprise when chunk 3 starts.

### 1.2 v2 never specified near-misses

The brief says *"v2 specified near-misses and never delivered them."* The string "near-miss" does not
appear anywhere in v2. What v2 specified was *abstention tests*, and of its two:

| id | question | verdict against your near-miss definition |
|---|---|---|
| q09 | "What is this book's ISBN?" | **Out-of-domain.** ISBN is a book-metadata attribute the corpus never engages with. Correctly excluded from v3. |
| q10 | "What year did Galileo, the author's employer, raise its Series A funding round?" | **Already a genuine near-miss.** The corpus establishes Galileo as the author's employer (q07's evidence); it just never discusses funding. |

So the shortfall is real but smaller than stated: one of two was out-of-domain, not both. q10 is worth
carrying into v3 as a near-miss with `near_miss_to` pointing at the employer passage — it is exactly
the shape you are asking for.

---

## 2. Schema

New file, `tests/fixtures/golden_set_v3.json`. **v2 is left in place**, because chunk 3 re-baselines
against v3 and v2 must stay readable for comparison.

```json
{
  "version": 3,
  "supersedes": 2,
  "corpus": {
    "document_id": "doc_01M0D39WZDYY7STA3PHWT5R4C7",
    "filename": "Mastering RAG 2026_compressed.pdf",
    "pages": 247,
    "chunks": 260,
    "anchoring": "canonical-text offsets, resolved through the containing chunk; no chunk_ids stored"
  },
  "entries": [
    {
      "id": "a01",
      "answerable": true,
      "question": "...",
      "document_id": "doc_01M0D39WZDYY7STA3PHWT5R4C7",
      "char_start": 5479,
      "char_end": 5509,
      "snippet": "1 million token context window",
      "page": 8,
      "expected_answer_substring": "...",
      "authoring_note": "how the question was framed away from the passage's own wording"
    },
    {
      "id": "u01",
      "answerable": false,
      "question": "...",
      "document_id": "doc_01M0D39WZDYY7STA3PHWT5R4C7",
      "near_miss_to": { "char_start": 0, "char_end": 0, "snippet": "the adjacent passage" },
      "why_unanswerable": "corpus states X; question asks Y",
      "absence_evidence": "ILIKE and tsquery probes that returned zero rows"
    }
  ]
}
```

`answerable` is a required boolean on every entry — the metric split is in the schema, as specified,
not inferred from a `type` string the way v2 did it.

---

## 3. Authorship method — the anti-leakage part

This is the part of the chunk that actually decides whether chunk 3's re-baseline means anything, so
it gets a method rather than good intentions.

**Procedure per question:** read the passage → look away from it → write down what a reader who does
*not* have the book open would type into a search box → only then check the passage again to confirm
the answer is really there. The question is authored from the information need, not from the
sentence.

**Overlap metric.** Computed in Postgres with `to_tsvector('english', ...)` — deliberately the *same
stemmer the lexical arm uses*, so the number measures the thing that actually inflates BM25, not an
approximation of it:

```
overlap = |lexemes(question) ∩ lexemes(snippet)| / |lexemes(question)|
```

**Reported as a full distribution**, per question, in the review table — as **both the ratio and the
raw intersection count**. The ratio alone is misleading on short questions: a 4-lexeme question
sharing 2 lexemes scores 0.50 with no leakage present, and a ratio-only flag column would fill with
false positives. The pair `2/4` and `0.50` together says what neither says alone.

**Flagged entries are resolved during review, not merely displayed.** Anything above **0.30** is
either rewritten before the table is presented, or carries an explicit one-line justification for why
the overlap is irreducible (a technical term with no lay synonym). No entry reaches you flagged and
unexplained.

**Honest limit, stated up front:** zero overlap is neither achievable nor desirable. You cannot ask
about cross-encoder rerankers without the word "reranker". The target is no *gratuitous* overlap —
no reused adjectives, no borrowed sentence structure, no copied numerals-plus-units. A question
scoring 0.0 would usually mean it is about something else. I will not tune questions to hit a number;
I will report the number and let you judge.

**Spread.** The corpus divides cleanly into 10 page-deciles of 24–28 chunks each (measured). Target
**3 answerable questions per decile** = 30, and the review table reports the achieved distribution so
clustering is visible rather than claimed.

---

## 4. Near-misses — 6 entries

A near-miss is a question the corpus *almost* answers. Construction rule: find a passage that states
X and is silent on a closely-related Y that a reader would reasonably expect alongside it. `near_miss_to`
points at the X passage; `why_unanswerable` states X-vs-Y in one line.

Absence is proved the way v2 proved it — that method was sound and is worth keeping: `text ILIKE`
plus `content_tsv @@ websearch_to_tsquery(...)`, both returning zero rows across all 260 chunks, with
the exact probes recorded in `absence_evidence`.

> **Known limit of this method — recorded, not fixed.** `absence_evidence` proves **lexical** absence
> only. A near-miss whose subject is present in the corpus under different wording would pass both the
> ILIKE and the tsquery probes and still be a wrong entry: the question would be marked unanswerable
> while the corpus quietly answers it in other words. The probes cannot detect that; only reading the
> surrounding material can, which is why near-misses are authored from passages actually read rather
> than from probe results alone. This is a floor on confidence in the 6 unanswerable entries, not a
> guarantee, and it is worth re-checking if any of them ever behaves oddly in a benchmark.

Anything that turns out to be absent from the domain entirely gets discarded and replaced, not
downgraded into the set.

---

## 5. `scripts/verify_golden_set.py`

New file; `scripts/` does not yet exist in this repo. Pure stdlib + SQLAlchemy, no new dependency.

Checks, per the spec:

| # | Check |
|---|---|
| 1 | `document_id` exists in `documents` — unknown id is a failure |
| 2 | A single chunk contains `[char_start, char_end)`; none found (span crosses a gap) is a failure |
| 3 | Text extracted at those offsets **equals `snippet` exactly** — byte for byte, no normalisation |
| 4 | `answerable: false` → has both `near_miss_to` and `why_unanswerable` |
| 5 | `answerable: true` → has **neither** |
| 6 | `near_miss_to` spans get checks 2 and 3 as well |

Collects every failure and prints all of them, then exits non-zero. No early return on first error.
Exits 0 and prints a one-line summary when clean.

> **Approval note:** vault gate #7 covers running scripts from `scripts/`. This is a new,
> read-only script in the code repo rather than the vault, and it only issues SELECTs — but I will
> ask before the first run rather than assume the gate does not apply.

---

## 6. Cost

**This chunk: $0.** Corpus reading is Postgres SELECTs, overlap scoring is `to_tsvector` in Postgres,
verification is SELECTs. No embedding call, no re-embed, nothing touches the 260 vectors.

**Chunk 3 will cost something, and you should see the number now.** The query-embedding cache in
`tests/fixtures/query_embeddings.json` is keyed by question content, so 36 new questions are 36 cache
misses. Extrapolating from the measured figure in `evaluation.py`'s docstring (13 questions ≈ 330
tokens, one request):

| | |
|---|---|
| ~36 questions | ~900 tokens, one Voyage request |
| at $0.02/1M | **~$0.00002** |
| free tier | 200M tokens — comfortably inside it |

Below the throttled account's 3 RPM / 10K TPM caps in a single request. **I will still stop and ask
before that call**, per the brief; it happens in chunk 3, not here.

---

## 7. Order of work, and where I stop

1. Survey the corpus across all 10 page-deciles (Postgres reads, $0)
2. Draft 30 answerable + 6 near-miss entries
3. Write `scripts/verify_golden_set.py`
4. Run it; fix entries until clean *(asks for the gate-#7 yes first)*
5. Present the review table: question · target snippet · overlap · decile, and the 6 near-misses
   shown side by side with their adjacent passage
6. **STOP.** Nothing measures against v3 — no `evaluate()`, no benchmark, no embedding — until you
   approve and the approval is recorded in `09_Memory/DECISIONS.md`.

---

## 8. Decisions needed before I draft

| # | Decision | My recommendation |
|---|---|---|
| D1 | Offsets resolved through the containing chunk (§0), since canonical text is unrecoverable | Yes — it is exact, verified, and keeps document-level offsets |
| D2 | v3 stores **no** `chunk_ids`; gold ids resolved from offsets at eval time, requiring a small `evaluation.py` change in chunk 3 (§1.1) | Yes — otherwise v3 dies on the next reindex exactly as v2 did |
| D3 | Carry v2's q10 (Galileo funding) into v3 as a near-miss; drop q09 (ISBN) as out-of-domain (§1.2) | Yes |
| D4 | Overlap > 0.30 gets flagged for rewrite in the review table, not auto-rejected (§3) | Yes — a hard threshold would push me to game the metric |
| D5 | New file `golden_set_v3.json`; v2 left untouched | Yes — chunk 3 needs both to compare |
