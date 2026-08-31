# RAG Reliability System

Document Q&A built as a reliability system, not a chatbot. The differentiator is
not that it answers — it is that it reports how much to trust the answer, and
refuses when the retrieved evidence does not support a claim. Most of the work in
this repository is spent on knowing when the system is wrong.

---

## The architecture

The design is a five-stage pipeline. Each stage exists to remove one specific way
a RAG system produces confident nonsense.

```
Planner  ->  Retriever  ->  Guardrail  ->  LLM  ->  Verifier
```

**Planner** — decides what to retrieve before retrieving it. A single embedding of
the raw user question is a poor query for anything compound; the planner exists so
that "compare X and Y" becomes two retrievals rather than one blurred average of
both.

**Retriever** — hybrid retrieval over the corpus: Postgres full-text search and
vector search run independently and are fused by Reciprocal Rank Fusion, then
reordered by a local cross-encoder. Two retrievers rather than one because lexical
search finds exact terms that embeddings smooth away, and vector search finds
paraphrases that no keyword matches. RRF fuses by rank rather than score because
`ts_rank_cd` and cosine distance are not on comparable scales.

**Guardrail** — inspects retrieved content before it reaches the model. Retrieved
chunks are untrusted input: a document can contain text written to manipulate the
model that reads it. This stage is also where a query with no adequate supporting
evidence is stopped rather than answered.

**LLM** — generates an answer constrained to the retrieved spans.

**Verifier** — scores the generated answer against the evidence that was actually
retrieved, and attaches citations that map back to source character offsets. An
answer whose claims do not appear in its evidence is not returned as an answer.

The reasoning above is the design. What is implemented today is a subset.

## What is built

| Component                                              | Status                       | Where                                                  |
| ------------------------------------------------------ | ---------------------------- | ------------------------------------------------------ |
| Ingestion — parse, chunk, embed, persist               | Built                        | `app/services/ingestion.py`, `chunking.py`, `parsing/` |
| Retriever — lexical, vector, RRF fusion                | Built                        | `app/services/retrieval.py`                            |
| Reranker — local int8 ONNX cross-encoder               | Built                        | `app/services/reranking.py`                            |
| Evaluation harness — recall@k, MRR, abstention scoring | Built                        | `app/services/evaluation.py`                           |
| Document state machine, status transitions             | Built                        | `app/documents/status.py`                              |
| CLI — ingest, query, stats, evaluate, reindex, delete  | Built                        | `app/cli.py`                                           |
| HTTP API                                               | Health endpoints only        | `app/api/v1/`                                          |
| **Planner**                                            | **Planned, not implemented** | `app/agents/` is empty                                 |
| **Guardrail — injection blocking, abstention**         | **Planned, not implemented** | `app/agents/` is empty                                 |
| **LLM answer generation**                              | **Planned, not implemented** | `app/agents/` is empty                                 |
| **Verifier — groundedness scoring, citations**         | **Planned, not implemented** | `app/agents/` is empty                                 |

`app/agents/` currently contains an `__init__.py` and nothing else. No groundedness
score is computed anywhere in this repository today. No prompt injection check runs
anywhere in this repository today. No answer is generated; the `query` command
returns ranked source chunks, not prose. The four agent stages are a design this
codebase is being built toward, and the retrieval layer beneath them is what
currently works.

`LLM_API_KEY` is required by configuration but no request is ever sent with it —
its presence is checked at startup and nothing more.

## Design commitments

These shape the code more than any framework choice does.

**Fail loudly rather than degrade silently.** If `RERANKER=local` and the weights
are missing or fail their checksum, the process aborts at startup. It never falls
back to no-reranking. An answer that looks reranked and is not is worse than a
process that refuses to start.

**Paid calls require explicit consent.** Embedding costs money, so every code path
that can spend is gated behind a flag that defaults to off. `ingest` refuses
without `--allow-network`; the evaluation harness raises on a query-embedding cache
miss rather than calling the API. The gate always runs before any destructive step,
so a refusal has no side effects and is safe to retry.

**Ground truth is anchored to content, not to generated identifiers.** The golden
set stores character offsets into a document's canonical text and resolves chunk
identifiers at evaluation time. Storing chunk ids directly meant one reindex
silently zeroed the gold set while the metrics kept reporting plausible numbers.

**Measurements are recorded as they came out.** Where a result contradicted the
prediction that motivated the work, it is recorded flat. See Measured results.

---

## Requirements

- Python >=3.12,<3.13. The upper bound is enforced by `pyproject.toml`.
- [uv](https://docs.astral.sh/uv/)
- Docker, for the Postgres 16 container in `docker-compose.dev.yml`
- `gitleaks` on `PATH`, used by the pre-commit secret scan
- A Voyage AI API key. This is a paid API and is called live on every `query` that
  uses the vector or hybrid arm, and on every `ingest`.
- An LLM provider key. Required by configuration, never used — see above.

## Setup

Nine steps from a clean clone to a working `query`. Each is required.

```bash
# 1. Clone
git clone https://github.com/Asaad-Suliman/rag-reliability-system.git
cd rag-reliability-system

# 2. Dependencies. uv provisions Python 3.12 if it is not present.
uv sync

# 3. Environment. Fill in DATABASE_URL, VOYAGE_API_KEY, LLM_API_KEY,
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

### Step 6 in detail

`Settings.reranker` defaults to `local`, and the weights are deliberately not
committed. Without this step, `query` exits 4:

```
refused: reranker weights in /.../models/reranker did not verify against
scripts/reranker_model.sha256:
  missing: /.../models/reranker/model.onnx
  missing: /.../models/reranker/tokenizer.json
  missing: /.../models/reranker/config.json

Run: uv run python scripts/fetch_reranker_model.py
```

The script is the only network path to these files. `app/services/reranking.py`
imports no HTTP client and no model-hub library, so at runtime there is no code
path that could fetch a missing weight — the guarantee is the absence of the
capability, not a flag. Files are fetched by commit SHA rather than by branch, then
verified byte for byte against the tracked manifest at
`scripts/reranker_model.sha256`.

**This step is not needed for `ingest`, `stats`, `delete`, or `evaluate`.** The
reranker is constructed per command, so those paths never load weights and never
fail on a clone that has not fetched them. `evaluate` defaults to no reranking
regardless of the environment variable, so that the recorded benchmark figures keep
reproducing; pass `--reranker local` to turn it on there.

### What each skipped step looks like

| Skipped              | Result                                                                                                                                                                                                                            |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 3, `.env`            | `environment error: ...`, exit 3. `DATABASE_URL`, `LLM_API_KEY` and `VOYAGE_API_KEY` have no defaults. Docker Compose aborts separately if the `POSTGRES_*` values are unset.                                                     |
| 4, Postgres          | `unavailable: ...`, exit 3, from the CLI preflight check                                                                                                                                                                          |
| 5, migrations        | `refused: postgres has no document_status enum type — is migration 0002 applied?`, exit 4. A reachable database with the wrong schema is a correctness problem, not an availability one, so it is not reported as unavailability. |
| 6, weights           | `refused: ...`, exit 4, with the message above                                                                                                                                                                                    |
| 7, ingest directory  | `error: ... is outside the allowed ingest root ...`, exit 2. `data/uploads` is a security boundary; files outside it are rejected before they are opened.                                                                         |
| 8, `--allow-network` | `refused: ...`, exit 4, with nothing written                                                                                                                                                                                      |

---

## Using it

The CLI is the working interface. The HTTP application currently serves health
endpoints only; there is no query endpoint yet.

```bash
uv run python -m app.cli ingest data/uploads/file.pdf --allow-network
uv run python -m app.cli query "what does the corpus say about chunking?"
uv run python -m app.cli query "..." --arm vector --top-k 10 --reranker none
uv run python -m app.cli stats
uv run python -m app.cli evaluate
uv run python -m app.cli reindex <doc_id> --file data/uploads/file.pdf --allow-network
uv run python -m app.cli delete <doc_id> --yes
```

Exit codes are stable and scriptable:

| Code | Meaning                                                       |
| ---- | ------------------------------------------------------------- |
| 0    | Success                                                       |
| 1    | The document pipeline ended in a terminal failure state       |
| 2    | Usage or input error                                          |
| 3    | Postgres or Chroma unreachable, or the environment is invalid |
| 4    | A paid call or a missing prerequisite was refused             |

The HTTP application:

```bash
uv run uvicorn app.main:app --reload
curl -i localhost:8000/api/v1/health          # liveness
curl -i localhost:8000/api/v1/health/ready    # readiness, 503 when a dependency is down
```

Every response carries an `X-Request-ID` header, and the same id appears in every
JSON log line for that request.

### When a query returns nothing

Three different things can produce no hits, and they are reported differently.

A query that matched nothing on a populated corpus is a legitimate answer and
exits 0: `no results — the corpus is not empty; no chunk matched this query`. If a
`--document-id` filter was applied, the message says so, since that is the usual
reason an otherwise good query comes back empty.

An empty corpus exits 4 and names the fix. Nothing has been ingested, so the
question was never really asked — a missing prerequisite, which is what exit 4
already means here.

The two stores can also be empty independently, and that is reported as its own
case rather than as an empty corpus. Which one matters depends on the arm:
`--arm lexical` reads only Postgres and `--arm vector` reads only Chroma, so a
store the arm never touched is never reported. A Chroma collection that is empty
while Postgres holds chunks usually means `VOYAGE_MODEL` or `VOYAGE_DIMENSIONS`
changed — collections are named per model and dimensions, so the corpus needs
re-embedding under the new one — and the message says that rather than claiming
the corpus is empty.

## Known limitations

**Reranking degrades the strongest retrieval arm.** See Measured results. This is
an open finding, not a resolved one.

**Abstention scoring is incomplete.** Near-miss coverage depends on the retrieval
arm and on whether reranking is enabled, and the abstention signal is still a rank
fusion artifact rather than a calibrated relevance score. No abstention rate should
be read as a pass rate yet.

---

## Evaluation

The evaluation harness measures retrieval quality against a hand-authored golden
set: recall@k and MRR over answerable questions, with unanswerable near-miss
questions scored separately and never folded into either.

Near-miss questions test refusal. Each names a fact the corpus does not contain but
sits deliberately close to one it does. A near-miss only tests refusal if the
retriever actually surfaced the near-miss span; if it did not, the system stayed
silent because retrieval found nothing, which is a retrieval failure and is scored
as UNSCORED rather than as a correct refusal.

```bash
uv run python -m app.cli evaluate                    # no reranking, the recorded baseline
uv run python -m app.cli evaluate --reranker local
```

### The published figures cannot be reproduced, by anyone

This includes the author. The numbers below are a record of a measurement that can
no longer be re-run:

- The source document is not in this repository. `data/` is gitignored, so neither
  the ingested PDF nor the Chroma vector store is committed.
- The source PDF no longer exists anywhere, and the canonical text cannot be
  reconstructed from the stored chunks: 246 of 259 consecutive chunk pairs have
  gaps between them.
- `tests/fixtures/golden_set_v3.json` pins `document_id`
  `doc_01M0D39WZDYY7STA3PHWT5R4C7` and resolves every gold chunk by character
  offsets through that specific document's chunks. It does not resolve against any
  other corpus.
- `tests/fixtures/query_embeddings.json` is gitignored, so the cached query vectors
  do not ship either.

Run on a fresh clone, `evaluate` stops at gold-chunk resolution and names the
document it cannot find:

```
refused: no chunks in the live corpus for document 'doc_01M0D39WZDYY7STA3PHWT5R4C7'
```

The missing query-embedding cache is a second barrier behind that one.

Re-embedding is not a path around this. Re-embedding requires the document, and the
document is gone. A new user can ingest their own corpus and use every feature of
this system; they cannot reproduce the figures below, and neither can this
repository's author.

### Measured results

Golden set v3, n=30 answerable questions, 6 near-miss. `candidate_k=30`, `rrf_k=5`,
`rerank_n=40`, MRR cut at depth 10. Left half is no reranking, right half is the
local cross-encoder.

| arm     | recall@5 | recall@10 | recall@30 | mrr@10 |     | recall@5 | recall@10 | recall@30 | mrr@10 |
| ------- | -------- | --------- | --------- | ------ | --- | -------- | --------- | --------- | ------ |
| lexical | 0.233    | 0.400     | 0.567     | 0.142  | ->  | 0.500    | 0.533     | 0.567     | 0.489  |
| vector  | 0.833    | 0.900     | 1.000     | 0.700  | ->  | 0.733    | 0.867     | 1.000     | 0.624  |
| hybrid  | 0.667    | 0.867     | 0.967     | 0.445  | ->  | 0.733    | 0.867     | 0.967     | 0.621  |

**Reranking degraded vector-only retrieval.** recall@5 fell from 0.833 to 0.733 and
MRR@10 from 0.700 to 0.624. The cross-encoder pushes gold chunks out of the top 5
that the embedding already had ranked correctly.

This contradicts the prediction that motivated building the reranker, which was
that it would recover the precision RRF costs. It does exactly that for the arms
RRF damaged — lexical gains 0.347 MRR, hybrid 0.176 — and costs accuracy on the arm
that was already strongest. Recorded as measured.

At n=30, one question is worth 3.3 recall points. After reranking, hybrid (0.621)
and vector (0.624) sit 0.003 apart, which is not a distinguishable difference at
this sample size.

**The cross-encoder dominates arm choice at rank 1.** Measured on the 6 near-miss
questions with reranking enabled: the lexical and vector candidate pools overlap by
as little as Jaccard 0.07, yet all three arms return an identical top-1 chunk on
every one of the 6 questions.

Identical does not mean correct. On 5 of the 6 — u01 through u05 — that shared
top-1 is the gold chunk, and those 5 are SCORED. On u06 all three arms also agree,
but the chunk they agree on is the wrong one, and u06 is UNSCORED on every arm.
Its gold chunk is absent from the lexical candidate pool entirely, and sits at
pre-rerank rank 2 in vector and rank 8 in hybrid; the reranker put a different
chunk first in all three. The convergence is 6 of 6; the success is 5 of 6.

On u05 specifically, the gold chunk sat at pre-rerank ranks 12 (lexical), 17
(vector) and 9 (hybrid), and the cross-encoder lifted it to rank 1 in all three —
which is why u05 is SCORED with reranking and UNSCORED without it on every arm.

Choosing between lexical, vector and hybrid has little effect on what reaches rank
1 once this reranker runs. That holds whether the chunk it selects is the right one
or not.

---

## Repository layout

```
app/
├── main.py          # app factory, lifespan, middleware, exception handlers
├── cli.py           # ingest, query, reindex, stats, evaluate, delete
├── api/v1/          # routers — health only
├── core/            # config, logging, errors, middleware, security
├── db/              # async engine, session, models, id generation
├── documents/       # document status state machine
├── services/
│   ├── parsing/     # PDF, DOCX, text, format sniffing
│   ├── chunking.py  # canonical-text offsets, overlap
│   ├── embeddings.py
│   ├── ingestion.py # the full parse -> chunk -> embed -> persist state machine
│   ├── retrieval.py # lexical, vector, RRF fusion
│   ├── reranking.py # int8 ONNX cross-encoder, checksum verification
│   ├── evaluation.py# golden-set harness
│   └── vector_store.py
├── schemas/         # Pydantic request/response models
└── agents/          # empty — Planner, Guardrail, LLM, Verifier are not built
alembic/             # migrations; the database URL comes from Settings, never the .ini
scripts/             # reranker weight fetch and checksum manifest
tests/fixtures/      # golden sets
docs/                # baseline measurements and design records
```

## Conventions

- **Error envelope** — all 4xx/5xx responses return
  `{"error": {"code", "message", "request_id", "details"}}`. Never a traceback.
- **Secrets** — `DATABASE_URL`, `LLM_API_KEY` and `VOYAGE_API_KEY` have no
  defaults. A missing one stops the process at startup with a readable message.
- **One collection per embedding model** — the Chroma collection name is derived
  from the model and its dimensions, so switching models cannot silently mix
  incompatible vector spaces.

## Quality gates

```bash
uv run ruff check .
uv run mypy
uv run pre-commit run --all-files
gitleaks git --no-banner
```

Several modules carry executable self-checks that assert behaviour rather than
describe it, including a digest that pins the no-reranking retrieval path
against the implementation from before the reranker existed:

```bash
uv run python -m app.services.retrieval
uv run python -m app.services.evaluation
uv run python -m app.services.reranking
```

The vector arm's cross-process determinism is checked separately, because a
self-check inside one process structurally cannot see it — the defect it guards
against was fixed at index-load time and varied between processes. It spawns 12
of them and takes ~30s:

```bash
uv run python -m tests.test_vector_search_stability
```

---

## Deployment gaps

Scope: the local development stack. The production stack is not built.

| Gap                      | At 1 demo user                                                             | At 1000 concurrent users                                                                                                                         | Mitigation                                                                                                                               |
| ------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| DB connection pool       | `pool_size=10 + max_overflow=5` = 15 connections per worker process, ample | 4 uvicorn workers = 60 connections against Postgres' default `max_connections=100`; more workers exhaust it and requests block on `pool_timeout` | Put PgBouncer (transaction pooling) in front, then keep the app pool small on purpose. Deferred to deployment.                           |
| Chroma persistent client | Embedded, single process, fine                                             | Each worker opens its own client over the same directory — no shared cache and no cross-process write coordination                               | Switch to Chroma in server mode. `VectorStore` is an interface precisely so this swap touches one file.                                  |
| Reranker memory          | One 22.8 MiB int8 model loaded once per process, fine                      | Loaded per worker, and inference runs one query-passage pair at a time by design, so throughput does not scale within a process                  | Move reranking to a separate service with its own pool. Batching is not the answer — it changes scores, see `app/services/reranking.py`. |
| LLM readiness check      | Config presence only, costs nothing                                        | Reports healthy while the provider may be down                                                                                                   | Real upstream probe with a short timeout and a cached result.                                                                            |
| Rate limiting            | Not implemented                                                            | Absent limits let one client exhaust the connection pool and the embedding budget                                                                | Not yet implemented.                                                                                                                     |
