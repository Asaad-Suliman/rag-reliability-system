# RAG Reliability System

RAG over a document corpus (hybrid lexical + vector retrieval, local cross-encoder reranker, LLM generation) built to measure when its own answers should not be trusted. Injection-defence experiments were pre-registered in `docs/DECISIONS.md` before any code ran.

- **Retrieval** (golden set v3, 30 answerable questions): reranking lowered the vector arm (recall@5 0.833 → 0.733, MRR@10 0.700 → 0.624) while improving lexical (MRR@10 0.142 → 0.489) and hybrid (0.445 → 0.621).
- **Injection:** `injection_scanner` v1 is a report-only tripwire (logs, never blocks, recall unknown); it finds 0 header/fence and 2 instruction-override matches on the 260-chunk corpus. `_neutralise` defangs all 8 tested fence-tag variants (old code: 0/8); header-open defang strips spoofed headers at render (fixtures: 8 header matches → 5, one per real header). Gated predictions passed; chunk (c) stopped at T2 as its stop rule required and passed after a pre-registered amendment.
- **Not established:** effect against a live model (all tests use an offline fake LLM); gate re-runs after chunk (b), RB-03 and chunk (c) are pending.

Golden set v3, n=30 answerable questions, 6 near-miss. `candidate_k=30`, `rrf_k=5`,
`rerank_n=40`, MRR cut at depth 10. The left half is no reranking and the right half is the local
cross-encoder.

| arm     | recall@5 | recall@10 | recall@30 | mrr@10 |     | recall@5 | recall@10 | recall@30 | mrr@10 |
| ------- | -------- | --------- | --------- | ------ | --- | -------- | --------- | --------- | ------ |
| lexical | 0.233    | 0.400     | 0.567     | 0.142  | ->  | 0.500    | 0.533     | 0.567     | 0.489  |
| vector  | 0.833    | 0.900     | 1.000     | 0.700  | ->  | 0.733    | 0.867     | 1.000     | 0.624  |
| hybrid  | 0.667    | 0.867     | 0.967     | 0.445  | ->  | 0.733    | 0.867     | 0.967     | 0.621  |

The analysis of these figures is under [Measured results](#measured-results).

---

Document Q&A built as a reliability system, not a chatbot. The goal is not only to answer, but to
report how much to trust the answer and to refuse when the retrieved evidence does not support a
claim. Most of the work in this repository goes into knowing when the system is wrong.

## The architecture

The design is a five-stage pipeline. Each stage exists to remove one specific way a RAG system
produces confident nonsense.

```
Planner  ->  Retriever  ->  Guardrail  ->  LLM  ->  Verifier
```

**Planner** (designed, not yet built). Decides what to retrieve before retrieving it. A single
embedding of the raw user question is a poor query for anything compound. The planner exists so
that "compare X and Y" becomes two retrievals rather than one blurred average of both. No planner
code exists.

**Retriever** (built). Hybrid retrieval over the corpus: Postgres full-text search and vector
search run independently, are fused by Reciprocal Rank Fusion, and are then reordered by a local
cross-encoder. There are two retrievers because lexical search finds exact terms that embeddings
smooth away, and vector search finds paraphrases that no keyword matches. RRF fuses by rank rather
than score because `ts_rank_cd` and squared L2 distance are not on comparable scales.

**Guardrail** (partly built). The single name was retired on 2026-09-04 and split into separate
mechanisms, each with its own status:

- `far_field_gate` (built) refuses a query whose nearest passage is too far away for the corpus to
  cover it.
- `injection_scanner` v1 (built) scans retrieved chunks for prompt-injection text before they
  reach the model. It is report-only: it logs, and it never blocks.
- `_neutralise` in `app/services/generation.py` (built) defangs the `<retrieved_context>` fence
  tags, including case and whitespace variants, in the question and the context.
- The header-open defang in `app/services/provenance.py` (built) defangs spoofed provenance-header
  openings in chunk bodies at render time.
- `relevance_floor`, a reranker-score threshold, is not built.
- Query-text scanning and output scanning are reserved and not started.

**LLM** (built). Generates a plain-text answer from the budgeted, rendered retrieved passages,
using the versioned prompt `app/agents/prompts/answer_v1.md`. It makes one attempt with no
retries. Any stop reason other than `end_turn`, or an empty completion, is a failure (503), never
a partial answer.

**Verifier** (designed, not yet built). Scores the generated answer against the evidence that was
actually retrieved, and attaches citations that map back to source character offsets. An answer
whose claims do not appear in its evidence would not be returned as an answer. Its design is
pre-registered in `docs/DECISIONS.md` (Chunk 8.11, "no code"). No Verifier code exists, so no
groundedness score is computed anywhere, and every generated answer is returned with the verdict
`ANSWER_UNVERIFIED`.

## What is built

| Component                                              | Status                       | Where                                                  |
| ------------------------------------------------------ | ---------------------------- | ------------------------------------------------------ |
| Ingestion — parse, chunk, embed, persist               | Built                        | `app/services/ingestion.py`, `chunking.py`, `parsing/` |
| Retriever — lexical, vector, RRF fusion                | Built                        | `app/services/retrieval.py`                            |
| Vector store — exact search over a frozen corpus       | Built, read-only             | `app/services/vector_store.py`, `app/corpus/`          |
| Reranker — local int8 ONNX cross-encoder               | Built                        | `app/services/reranking.py`                            |
| Evaluation harness — recall@k, MRR, abstention scoring | Built                        | `app/services/evaluation.py`                           |
| Document state machine, status transitions             | Built                        | `app/documents/status.py`                              |
| CLI — ingest, query, stats, evaluate, reindex, delete  | Built                        | `app/cli.py`                                           |
| HTTP API — health, readiness, `POST /query`            | Built                        | `app/api/v1/`                                          |
| Client credential, per-minute limit, daily cap         | Built, in-process            | `app/core/security.py`                                 |
| Context budget and provenance rendering                | Built                        | `app/services/context_budget.py`, `provenance.py`      |
| LLM answer generation                                  | Built                        | `app/services/generation.py`                           |
| Far-field gate — out-of-domain refusal                 | Built                        | `app/services/retrieval.py`                            |
| Injection scanner — retrieved chunks, report-only      | Built (v1)                   | `app/agents/injection_scanner.py`                      |
| **Planner**                                            | **Designed, not yet built**  | —                                                      |
| **Guardrail**                                          | **Partly built** (see above) | —                                                      |
| **Verifier — groundedness scoring, citations**         | **Designed, not yet built**  | —                                                      |

The `query` CLI command returns ranked source chunks, not prose. Only `POST /api/v1/query`
generates an answer, and it calls the model only when the far-field gate does not refuse.

## Design commitments

These shape the code more than any framework choice does.

**Fail loudly rather than degrade silently.** If `RERANKER=local` and the weights are missing or
fail their checksum, the process aborts at startup. It never falls back to no reranking. An answer
that looks reranked but is not is worse than a process that refuses to start.

**Paid calls require explicit consent.** Embedding costs money, so every code path that can spend
is gated behind a flag that defaults to off. `ingest` refuses without `--allow-network`, and the
evaluation harness raises on a query-embedding cache miss rather than calling the API. The gate
always runs before any destructive step, so a refusal has no side effects and is safe to retry.

**Ground truth is anchored to content, not to generated identifiers.** The golden set stores
character offsets into a document's canonical text and resolves chunk identifiers at evaluation
time. Storing chunk ids directly once meant a single reindex silently zeroed the gold set while the
metrics kept reporting plausible numbers.

**Measurements are recorded as they came out.** When a result contradicted the prediction that
motivated the work, it is recorded plainly. See [Measured results](#measured-results).

---

## Requirements

- Python >=3.12,<3.13. The upper bound is enforced by `pyproject.toml`.
- [uv](https://docs.astral.sh/uv/)
- Docker, for the Postgres 16 container in `docker-compose.dev.yml`
- `gitleaks` on `PATH`, used by the pre-commit secret scan
- A Voyage AI API key. This is a paid API. It is called live on every `query` that uses the vector
  or hybrid arm, and on every `ingest`.
- An Anthropic API key (`LLM_API_KEY`). It is used by `POST /api/v1/query` to generate answers.

## Setup

Nine steps from a clean clone to a working `query`. Each is required.

```bash
# 1. Clone
git clone https://github.com/Asaad-Suliman/rag-reliability-system.git
cd rag-reliability-system

# 2. Dependencies. uv provisions Python 3.12 if it is not present.
uv sync

# 3. Environment. Fill in DATABASE_URL, VOYAGE_API_KEY, LLM_API_KEY, CLIENT_API_KEY,
#    and the POSTGRES_* values, which must match DATABASE_URL.
cp .env.example .env

# 4. Postgres
docker compose -f docker-compose.dev.yml up -d

# 5. Schema
uv run alembic upgrade head

# 6. Reranker weights — 22.8 MiB, not in git. See below.
uv run python scripts/fetch_reranker_model.py

# 7. Ingest directory. Gitignored, so a fresh clone does not have it.
mkdir -p data/uploads
cp /path/to/your-document.pdf data/uploads/

# 8. Ingest. --allow-network is required and spends Voyage tokens.
uv run python -m app.cli ingest data/uploads/your-document.pdf --allow-network

# 9. Query
uv run python -m app.cli query "your question here"
```

**Ingest does not work on the current code, because the vector store is frozen.** Since chunk 7.2,
the vector arm reads a tracked, read-only corpus in `app/corpus/` (260 vectors, pinned by
sha256). Its chunk text is not committed, so on a fresh clone step 9 (hybrid by default) is
refused; see [Reproducibility](#reproducibility). `ExactVectorStore` refuses every write. Step 8 therefore spends the Voyage embedding
call and is then refused with `CorpusUnavailableError` when it tries to store the vectors.
`reindex` and `delete` are refused the same way. Adding a new document means re-embedding it
under a writable store and re-running `scripts.export_vectors`, as the error message says.

### Step 6 in detail

`Settings.reranker` defaults to `local`, and the weights are deliberately not committed. Without
this step, `query` exits 4:

```
refused: reranker weights in /.../models/reranker did not verify against
scripts/reranker_model.sha256:
  missing: /.../models/reranker/model.onnx
  missing: /.../models/reranker/tokenizer.json
  missing: /.../models/reranker/config.json

Run: uv run python scripts/fetch_reranker_model.py
```

The script is the only network path to these files. `app/services/reranking.py` imports no HTTP
client and no model-hub library, so at runtime no code path can fetch a missing weight. The
guarantee comes from the absence of that capability, not from a flag. Files are fetched by commit
SHA rather than by branch, then verified byte for byte against the tracked manifest at
`scripts/reranker_model.sha256`.

**This step is not needed for `ingest`, `stats`, `delete`, or `evaluate`.** The reranker is
constructed per command, so those paths never load weights and never fail on a clone that has not
fetched them. `evaluate` defaults to no reranking regardless of the environment variable, so that
the recorded benchmark figures stay comparable. Pass `--reranker local` to turn it on there.

### What each skipped step looks like

| Skipped              | Result                                                                                                                                                                                                                            |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3, `.env`            | `environment error: ...`, exit 3. `DATABASE_URL`, `LLM_API_KEY`, `VOYAGE_API_KEY` and `CLIENT_API_KEY` have no defaults. Docker Compose aborts separately if the `POSTGRES_*` values are unset.                                   |
| 4, Postgres          | `unavailable: ...`, exit 3, from the CLI preflight check                                                                                                                                                                          |
| 5, migrations        | `refused: postgres has no document_status enum type — is migration 0002 applied?`, exit 4. A reachable database with the wrong schema is a correctness problem, not an availability one, so it is not reported as unavailability. |
| 6, weights           | `refused: ...`, exit 4, with the message above                                                                                                                                                                                    |
| 7, ingest directory  | `error: ... is outside the allowed ingest root ...`, exit 2. `data/uploads` is a security boundary; files outside it are rejected before they are opened.                                                                         |
| 8, `--allow-network` | `refused: ...`, exit 4, with nothing written                                                                                                                                                                                      |

---

## Using it

The CLI retrieves and ranks. The HTTP API also generates.

```bash
uv run python -m app.cli ingest data/uploads/file.pdf --allow-network   # refused under the frozen store, see Setup
uv run python -m app.cli query "what does the corpus say about chunking?"
uv run python -m app.cli query "..." --arm vector --top-k 10 --reranker none
uv run python -m app.cli stats
uv run python -m app.cli evaluate
uv run python -m app.cli reindex <doc_id> --file data/uploads/file.pdf --allow-network
uv run python -m app.cli delete <doc_id> --yes
```

Exit codes are stable and scriptable:

| Code | Meaning                                                 |
| ---- | ------------------------------------------------------- |
| 0    | Success                                                 |
| 1    | The document pipeline ended in a terminal failure state |
| 2    | Usage or input error                                    |
| 3    | Postgres unreachable, or the environment is invalid     |
| 4    | A paid call or a missing prerequisite was refused       |

The HTTP application:

```bash
# --workers 1 and --host 127.0.0.1 are both load-bearing; see below.
uv run uvicorn app.main:app --reload --workers 1 --host 127.0.0.1

curl -i localhost:8000/api/v1/health          # liveness, no credential
curl -i localhost:8000/api/v1/health/ready    # readiness, 503 when a dependency is down

# /query needs the shared machine credential (CLIENT_API_KEY in .env, 32+ chars,
# generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))")
curl -i localhost:8000/api/v1/query \
  -H "X-API-Key: $CLIENT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"question": "What is a reranker?"}'
```

`POST /query` retrieves (hybrid, reranked) and runs the far-field gate. On
`ABSTAIN_OUT_OF_DOMAIN` it returns 200 with `answer: null`, the model is never called, and the
citations are the passages that triggered the refusal. Otherwise it budgets the context, runs the
injection scanner (report-only), renders the passages, and generates an answer. That answer comes
back as `ANSWER_UNVERIFIED`, with citations limited to the chunks the model actually saw.

Every response carries an `X-Request-ID` header, and the same id appears in every JSON log line for
that request. Every `/query` response where the limiter ran, whether 200 or 429, also carries
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`. A 429 also carries
`Retry-After`.

**`--workers 1` is not a default, it is a correctness requirement.** The per-minute limit (20)
and the daily cap (200 admitted requests, reset at UTC midnight) live in process memory. Each extra
worker gets its own counters, so N workers serve N times the limit and permit N times the daily
spend. Raising the worker count without first moving this state to a shared store (Redis) silently
multiplies the bill. The startup log states the limits and that they are per-process.

**`--host 127.0.0.1` stays until there is TLS.** The credential is a bearer secret. It travels in
a plain header, so on plain HTTP anyone on the path can read and replay it. Binding beyond
localhost is acceptable only when all four of these hold: `CLIENT_API_KEY` is set, the limits are
active, it runs as a single worker, and it sits behind TLS. TLS arrives in Step 05. Until then,
stay on loopback.

### When a query returns nothing

Three different things can produce no hits, and they are reported differently.

A query that matched nothing on a populated corpus is a legitimate answer and exits 0:
`no results — the corpus is not empty; no chunk matched this query`. If a `--document-id` filter
was applied, the message says so, since that is the usual reason an otherwise good query comes back
empty.

An empty corpus exits 4 and names the fix. Nothing has been ingested, so the question was never
really asked. That is a missing prerequisite, which is what exit 4 already means here.

The two stores can also be empty independently, and that is reported as its own case rather than
as an empty corpus. Which one matters depends on the arm. `--arm lexical` reads only Postgres and
`--arm vector` reads only the frozen vector corpus, so a store the arm never touched is never
reported. A vector corpus that reads as empty while Postgres holds chunks usually means
`VOYAGE_MODEL` or `VOYAGE_DIMENSIONS` changed. The corpus is named per model and dimensions, so it
needs re-embedding under the new one, and the message says that rather than claiming the corpus is
empty.

## Known limitations

**Reranking degrades the strongest retrieval arm.** See [Measured results](#measured-results).
This is an open finding, not a resolved one.

**Abstention scoring is incomplete.** Near-miss coverage depends on the retrieval arm and on
whether reranking is enabled. The abstention signal is still a rank-fusion artifact rather than a
calibrated relevance score, so no abstention rate should be read as a pass rate yet.

**Generated answers are not checked.** No Verifier exists, so nothing establishes that an answer
follows from its citations.

---

## Evaluation

The evaluation harness measures retrieval quality against a hand-authored golden set: recall@k and
MRR over answerable questions. Unanswerable near-miss questions are scored separately and never
folded into either metric.

Near-miss questions test refusal. Each asks about a fact the corpus does not contain but sits
deliberately close to one it does. A near-miss only tests refusal if the retriever actually
surfaced the near-miss span. If it did not, the system stayed silent because retrieval found
nothing. That is a retrieval failure, and it is scored as UNSCORED rather than as a correct
refusal.

```bash
uv run python -m app.cli evaluate                    # no reranking, the recorded baseline
uv run python -m app.cli evaluate --reranker local
```

### Reproducibility

The published figures can be re-run only on the author's machine; nobody can re-run them from
this repository alone. **Committed:** the 260 chunk vectors and their metadata (chunk id,
document id, page, character offsets) in `app/corpus/`, the golden set's questions and offsets,
and sha256 pins for every file held back. **Not committed:** the source PDF (a third-party book,
no longer available); the chunk text and the golden set's snippets and answer substrings (book
text, kept in the gitignored `data/corpus_text.json` and `data/golden_set_v3_text.json`); the
Postgres database holding the ingested chunks; and the cached query embeddings
(`tests/fixtures/query_embeddings.json`). Without the chunk text, vector and hybrid retrieval
refuse to run and the text-dependent tests skip. On a fresh clone, `evaluate` stops with
`refused: no chunks in the live corpus for document 'doc_01M0D39WZDYY7STA3PHWT5R4C7'`.

### Measured results

The table is at the [top of this file](#rag-reliability-system).

**Reranking degraded vector-only retrieval.** recall@5 fell from 0.833 to 0.733 and MRR@10 from
0.700 to 0.624. The cross-encoder pushes gold chunks out of the top 5 that the embedding had
already ranked correctly.

This contradicts the prediction that motivated building the reranker, which was that it would
recover the precision RRF costs. It does exactly that for the arms RRF damaged (lexical gains
0.347 MRR, hybrid 0.176), and it costs accuracy on the arm that was already strongest. Recorded
as measured.

At n=30, one question is worth 3.3 recall points. After reranking, hybrid (0.621) and vector
(0.624) sit 0.003 apart, which is not a distinguishable difference at this sample size.

**The cross-encoder dominates arm choice at rank 1.** This was measured on the 6 near-miss
questions with reranking enabled. The lexical and vector candidate pools overlap by as little as
Jaccard 0.07, yet all three arms return an identical top-1 chunk on every one of the 6 questions.

Identical does not mean correct. On 5 of the 6 (u01 through u05), that shared top-1 is the gold
chunk, and those 5 are SCORED. On u06 all three arms also agree, but the chunk they agree on is
the wrong one, and u06 is UNSCORED on every arm. Its gold chunk is absent from the lexical
candidate pool entirely. It sits at pre-rerank rank 2 in vector and rank 8 in hybrid, and the
reranker put a different chunk first in all three. The arms converge on 6 of 6 questions but
succeed on 5 of 6.

On u05, the gold chunk sat at pre-rerank ranks 12 (lexical), 17 (vector) and 9 (hybrid), and the
cross-encoder lifted it to rank 1 in all three. That is why u05 is SCORED with reranking and
UNSCORED without it on every arm.

Once this reranker runs, the choice between lexical, vector and hybrid has little effect on what
reaches rank 1. That holds whether the chunk it selects is the right one or not.

---

## Repository layout

```
app/
├── main.py          # app factory, lifespan, middleware, exception handlers
├── cli.py           # ingest, query, reindex, stats, evaluate, delete
├── api/v1/          # routers — health, query
├── core/            # config, logging, errors, middleware, security (credential, limits)
├── corpus/          # frozen vector corpus: 260 vectors + manifest, sha256-pinned; text is local-only
├── db/              # async engine, session, models, id generation
├── documents/       # document status state machine
├── services/
│   ├── parsing/          # PDF, DOCX, text, format sniffing
│   ├── chunking.py       # canonical-text offsets, overlap
│   ├── embeddings.py
│   ├── ingestion.py      # the full parse -> chunk -> embed -> persist state machine
│   ├── retrieval.py      # lexical, vector, RRF fusion, far-field gate
│   ├── reranking.py      # int8 ONNX cross-encoder, checksum verification
│   ├── context_budget.py # token budget, whole chunks only
│   ├── provenance.py     # renders passages with provenance headers, header-open defang
│   ├── generation.py     # Anthropic Messages API, fence-tag neutralisation
│   ├── evaluation.py     # golden-set harness
│   └── vector_store.py   # ExactVectorStore, read-only exact search
├── schemas/         # Pydantic request/response models
└── agents/          # injection_scanner.py, prompts/ — Planner and Verifier are not built
alembic/             # migrations; the database URL comes from Settings, never the .ini
scripts/             # reranker weight fetch and checksum manifest, measurement scripts
tests/fixtures/      # golden sets, injection-scanner fixtures
docs/                # baseline measurements and design records (DECISIONS.md)
```

## Conventions

- **Error envelope.** All 4xx/5xx responses return
  `{"error": {"code", "message", "request_id", "details"}}`, never a traceback.
- **Secrets.** `DATABASE_URL`, `LLM_API_KEY`, `VOYAGE_API_KEY` and `CLIENT_API_KEY` have no
  defaults. A missing one stops the process at startup with a readable message.
- **One corpus per embedding model.** The vector collection name is derived from the model and its
  dimensions, and `ExactVectorStore` refuses a query for any other collection, so switching models
  cannot silently mix incompatible vector spaces.

## Quality gates

```bash
uv run ruff check .
uv run mypy
uv run pre-commit run --all-files
gitleaks git --no-banner
```

Several modules carry executable self-checks that assert behaviour rather than describe it. One of
them is a digest that pins the no-reranking retrieval path against the implementation from before
the reranker existed:

```bash
uv run python -m app.services.retrieval
uv run python -m app.services.evaluation
uv run python -m app.services.reranking
```

The vector arm's cross-process determinism is checked separately, because a self-check inside one
process cannot see it: the defect it guards against varied between processes, not within one. The
check spawns 12 processes and takes ~30s:

```bash
uv run python -m tests.test_vector_search_stability
```

---

## Deployment gaps

Scope: the local development stack. The production stack is not built.

| Gap                 | At 1 demo user                                                             | At 1000 concurrent users                                                                                                                         | Mitigation                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| DB connection pool  | `pool_size=10 + max_overflow=5` = 15 connections per worker process, ample | 4 uvicorn workers = 60 connections against Postgres' default `max_connections=100`; more workers exhaust it and requests block on `pool_timeout` | Put PgBouncer (transaction pooling) in front, then keep the app pool small on purpose. Deferred to deployment.                           |
| Frozen vector store | 260 vectors, exact search, read-only, fine                                 | Cannot take new documents at all; exact search stops being cheap as N grows                                                                      | Swap in a writable backend behind the `VectorStore` interface; DECISIONS (chunk 7.2) records the measured ANN reintroduction threshold.  |
| Reranker memory     | One 22.8 MiB int8 model loaded once per process, fine                      | Loaded per worker, and inference runs one query-passage pair at a time by design, so throughput does not scale within a process                  | Move reranking to a separate service with its own pool. Batching is not the answer — it changes scores, see `app/services/reranking.py`. |
| LLM readiness check | Config presence only, costs nothing                                        | Reports healthy while the provider may be down                                                                                                   | Real upstream probe with a short timeout and a cached result.                                                                            |
| Rate limiting       | In-process: 20 requests/minute and 200/day, one worker, fine               | Counters are per worker, so N workers allow N times the limit and the spend                                                                      | Move limiter state to a shared store (Redis) before running more than one worker.                                                        |
