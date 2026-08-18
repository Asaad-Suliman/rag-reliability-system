# RAG Reliability System

Document Q&A built as a **reliability system**, not a chatbot. The differentiator
is not that it answers — it is that it reports how much to trust the answer, and
refuses when the retrieved evidence does not support a claim.

A four-agent pipeline (Planner → Retriever → Guardrail → LLM → Verifier) that
scores its own groundedness, blocks prompt injection, and ships every answer with
citations that map back to source spans.

> **Status: Step 01 — Repo Scaffold.** The skeleton boots and reports dependency
> health. There is no business logic yet: no retrieval, no agents, no auth.

---

## Stack

| Layer      | Choice                                       |
| ---------- | -------------------------------------------- |
| API        | Python 3.12 / FastAPI                        |
| Relational | PostgreSQL (SQLAlchemy 2 async + Alembic)    |
| Vector     | ChromaDB (persistent client)                 |
| Packaging  | uv                                           |
| Quality    | ruff · mypy (strict) · pre-commit · gitleaks |

---

## Prerequisites

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker (for the local Postgres in `docker-compose.dev.yml`)
- `gitleaks` on `PATH` (used by the pre-commit secret scan)

---

## Setup

```bash
cp .env.example .env          # then edit: set POSTGRES_PASSWORD, DATABASE_URL, LLM_API_KEY
uv sync                       # creates .venv and installs locked dependencies
uv run pre-commit install     # enables lint + type + secret hooks on commit

docker compose -f docker-compose.dev.yml up -d    # local Postgres on 127.0.0.1:5432
uv run alembic upgrade head                       # apply migrations
```

`.env` is gitignored and must stay that way. Never commit real credentials —
`.env.example` carries placeholders only.

## Run

```bash
uv run uvicorn app.main:app --reload
```

## Verify

```bash
curl -i localhost:8000/api/v1/health          # liveness  -> 200 {"status":"ok"}
curl -i localhost:8000/api/v1/health/ready    # readiness -> 200, all checks "ok"

# The readiness endpoint must degrade, not crash:
docker compose -f docker-compose.dev.yml stop postgres
curl -i localhost:8000/api/v1/health/ready    # -> 503, identical JSON shape
docker compose -f docker-compose.dev.yml start postgres
```

Every response — success or failure — carries an `X-Request-ID` header, and the
same id appears in every JSON log line for that request.

## Quality gates

```bash
uv run ruff check .
uv run mypy
uv run pre-commit run --all-files
gitleaks git --no-banner        # full history secret scan
```

---

## Layout

```
app/
├── main.py        # app factory, lifespan, middleware and exception handlers
├── api/v1/        # routers — /health, /health/ready
├── core/          # config, logging, errors, middleware, security
├── db/            # async engine + session, declarative base, models
├── agents/        # Planner, Retriever, Guardrail, Verifier   (Step 03)
├── services/      # VectorStore interface + Chroma implementation
└── schemas/       # Pydantic request/response models
alembic/           # migrations; the DB URL comes from Settings, never the .ini
```

## Conventions

- **API contract** is the source of truth for every response shape, error code
  and status enum. Changing it is a breaking change.
- **Error envelope** — all 4xx/5xx return
  `{"error": {"code", "message", "request_id", "details"}}`. Never a traceback.
- **Secrets** — `DATABASE_URL` and `LLM_API_KEY` have no defaults. A missing one
  stops the app at startup with a readable message.

---

## Deployment gaps (Step 01 scope)

| Gap                      | At 1 demo user                                                             | At 1000 concurrent users                                                                                                                         | Mitigation                                                                                                                   |
| ------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| DB connection pool       | `pool_size=10 + max_overflow=5` = 15 connections per worker process, ample | 4 uvicorn workers = 60 connections against Postgres' default `max_connections=100`; more workers exhaust it and requests block on `pool_timeout` | Put PgBouncer (transaction pooling) in front, then keep the app pool small on purpose. Deferred to deployment.               |
| Chroma persistent client | Embedded, single process, fine                                             | Each worker opens its own client over the same directory — no shared cache and no cross-process write coordination                               | Switch to Chroma in server mode. `VectorStore` is an interface precisely so this swap touches one file. Deferred to Step 05. |
| LLM readiness check      | Config presence only, costs nothing                                        | Says "ok" while the provider may be down                                                                                                         | Real upstream probe with a short timeout and a cached result. Step 02.                                                       |
| Rate limiting            | Not implemented                                                            | Absent limits let one client exhaust the pool and the LLM budget                                                                                 | Contract §7 limits. Step 04.                                                                                                 |
