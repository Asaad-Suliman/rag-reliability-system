# Step 03 · Chunk 1 — Status transition function

**Status:** plan only, awaiting approval. No code written.
**Scope gate:** `documents.status_changed_at` does **not** exist. Decision taken: **build without it (option A)**. No migration in this chunk.

---

## 0. Why no `status_changed_at`

`audit_events` already exists, is append-only, has `created_at` and a JSONB `payload`, and its
docstring already claims "Step 02 writes ingestion state transitions here". A `status_changed_at`
column would be a denormalised cache of `MAX(created_at)` over that table — a second source of truth
for something the first source already answers, kept in sync by hand.

The one thing a column buys that the audit table does not is a cheap sweeper query
("find every document stuck in `parsing` for > 10 minutes"). Nothing needs that sweeper yet.
Add the column when a sweeper is actually written, not before.

`transition()`'s signature does not reference it, so nothing in the requested API is weakened.

---

## 1. Files touched

| File | Change |
|---|---|
| `app/documents/__init__.py` | new, empty |
| `app/documents/status.py` | **new** — the whole module |
| `app/db/models/document.py` | add `DocumentStatus(StrEnum)`; derive `DOCUMENT_STATUSES` from it; `create_type=True` → `False` |
| `app/services/ingestion.py` | 9 status assignments → `transition()` calls; restructure the retry preamble; delete `_audit()`'s 5 transition-adjacent calls |
| `app/cli.py` | `_cmd_reindex` stops writing `status` entirely; `ingest_document(..., force=True)` |
| `app/main.py` | call the startup enum assertion in `lifespan` |

Six files. No new dependency. No migration. No Alembic revision.

> Note: `app/documents/` is a seventh top-level package alongside `api/ core/ db/ schemas/ services/ agents/`.
> `app/services/document_status.py` would fit the existing layout with zero new packages. You named
> `app/documents/status.py` explicitly, so that is what the plan builds — say the word if you want it
> under `services/` instead.

---

## 2. `DocumentStatus` — where the enum lives

Goes in `app/db/models/document.py`, next to the column it types, as a `StrEnum`:

```python
class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"

DOCUMENT_STATUSES = tuple(s.value for s in DocumentStatus)   # was a hand-written tuple
DocumentStatusType = PGEnum(*DOCUMENT_STATUSES, name="document_status", create_type=False)
```

Three deliberate choices:

- **`StrEnum`, not `Enum`.** Members compare equal to their string values, so every existing
  `doc.status == "ready"` in `ingestion.py`, `cli.py` and the `_demo()` blocks keeps working
  untouched. Zero-churn migration.
- **`PGEnum` still built from plain strings**, not from the enum class. Passing the class makes
  SQLAlchemy serialise `.name` (`"READY"`) unless you add `values_callable`; passing strings
  sidesteps that trap entirely.
- **`Mapped[str]` on the column stays as-is.** Typing it `Mapped[DocumentStatus]` needs
  `values_callable` and re-checks every comparison in the repo for a cosmetic gain. Not now.

**`create_type=False` is safe here:** `grep create_all` over `app/` and `alembic/` returns nothing —
the schema only ever comes from migrations. Migration `0002` builds its own `postgresql.ENUM(...)`
literal inline (line 100), independent of the model, so flipping the model flag cannot change what
already ran, and stops autogenerate emitting `CREATE TYPE` on the next revision.

---

## 3. The transition table and its derived inverse

```python
LEGAL_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    QUEUED:      frozenset({PARSING, QUARANTINED, FAILED}),
    PARSING:     frozenset({INDEXING, QUARANTINED, FAILED}),
    INDEXING:    frozenset({READY, FAILED}),
    READY:       frozenset({PARSING}),
    FAILED:      frozenset({QUEUED}),
    QUARANTINED: frozenset(),
}

LEGAL_PREDECESSORS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    to: frozenset(frm for frm, tos in LEGAL_TRANSITIONS.items() if to in tos)
    for to in DocumentStatus
}
```

One comprehension, evaluated at import. The table is written once.

Self-transitions are illegal for free: no state lists itself, so no state ever appears in its own
predecessor set. No guard needed.

Every one of the six states has at least one predecessor under this table, so `LEGAL_PREDECESSORS[to]`
is never empty in practice. If a future table edit makes one empty, `.in_(frozenset())` renders as an
always-false expression, the UPDATE matches zero rows, and the failure path raises
`IllegalStatusTransition` — which is the correct answer anyway. No special case.

---

## 4. `transition()`

```python
async def transition(
    session: AsyncSession,
    document_id: str,
    to: DocumentStatus,
    *,
    reason: str | None = None,
) -> Document:
```

**Happy path — one statement:**

```python
stmt = (
    update(Document)
    .where(Document.id == document_id, Document.status.in_(LEGAL_PREDECESSORS[to]))
    .values(status=to)
    .returning(Document)
    .execution_options(synchronize_session="fetch")
)
document = (await session.execute(stmt)).scalars().one_or_none()
```

- The legality check **is** the `WHERE` clause. There is no Python-side read of the current status
  before the write, so two workers cannot both pass a check and clobber each other.
- `synchronize_session="fetch"` matters and is not cosmetic: `ingest_document()` holds a live
  `Document` instance in the session's identity map. Without it, the returned object can carry the
  stale in-memory `status` while the row on disk has the new one. On Postgres, SQLAlchemy satisfies
  the "fetch" from the `RETURNING` rows rather than issuing a pre-`SELECT`, so this stays a single
  round trip.

**Failure path — and only the failure path — pays for a second query:**

```python
if document is None:
    current = (await session.execute(
        select(Document.status).where(Document.id == document_id)
    )).scalar_one_or_none()
    if current is None:
        raise DocumentNotFound(document_id)
    raise IllegalStatusTransition(document_id, DocumentStatus(current), to)
```

**`reason` goes to `audit_events`** — see §5. It is the only destination that is not dead.

**No commit.** `transition()` never commits or rolls back; the caller owns the transaction boundary,
exactly as `ingest_document()` does today.

**Concurrency semantics, stated explicitly.** Under Postgres READ COMMITTED, when worker B issues the
same `UPDATE ... WHERE status IN (...)` against a row worker A has already updated but not committed,
B *blocks* on A's row lock. When A commits, B re-evaluates its `WHERE` against the new row version,
finds `status` no longer in the predecessor set, matches zero rows, and raises
`IllegalStatusTransition`. Exactly one winner, no application-level lock, no retry loop. This is the
whole reason the check lives in SQL.

**Known, accepted race on the error path:** between the zero-row UPDATE and the follow-up SELECT, a
third party can change or delete the row, so the `from_status` in the raised error is a best-effort
snapshot and a deleted row surfaces as `DocumentNotFound` rather than `IllegalStatusTransition`. The
*kind* of answer is always right — the transition did not happen — only the diagnostic detail can
drift. Not worth a lock.

**Exceptions**, both in `status.py`:

```python
class DocumentNotFound(LookupError):        # document_id
class IllegalStatusTransition(ValueError):  # document_id, from_status, to_status (all attributes)
```

`from_status` / `to_status` are stored as attributes, not just formatted into the message, so Step 04
can map them onto contract error codes without parsing strings.

**Malformed `document_id` needs no validation.** It is compared, never stored, so a garbage or
over-length string simply matches no row in both queries → `DocumentNotFound`. Adding a ULID-shape
regex would buy a different exception for the same non-existent document.

---

## 5. Where `reason` goes

`transition()` writes one `audit_events` row per successful transition:

```python
session.add(AuditEvent(
    user_id=document.user_id,          # available from RETURNING, no extra query
    event_type=f"document_status_{to}",
    resource_type="document",
    resource_id=document.id,
    payload={"from": from_status, "to": to, "reason": reason},
))
```

`session.add()` buffers into the unit of work and flushes on the caller's existing `commit()` — no
extra round trip before commit.

This is a **consolidation, not an addition**: `ingestion.py` currently hand-writes five `_audit()`
calls immediately adjacent to status assignments (`ingest_parse_failed`, `ingest_no_chunks`,
`ingest_over_token_limit`, `ingest_embedding_failed`, `ingest_ready`). All five are deleted; their
specificity moves from `event_type` into `payload.reason`, which is what `reason` is for. Making the
choke point write the audit row is what turns "every status change is audited" from a convention into
a structural fact.

`_audit()` survives in `ingestion.py` with exactly one caller: the initial INSERT
(`document_created`), which is a row birth, not a transition, and so is not `transition()`'s business.

Nothing anywhere reads `event_type` — `grep AuditEvent` finds only the model, the export, and the
write site — so renaming the event types breaks no consumer. Step 04's API does not exist yet.

`document.error` stays a caller concern. It is user-facing copy shown in the CLI, not a transition
reason, and `transition()` does not touch it.

> **Decision D3.** If you would rather `reason` only hit the structured log and leave `ingestion.py`'s
> five `_audit()` calls alone, say so — it is a smaller diff, but `event_type` stays ad-hoc and the
> audit trail stays a convention.

---

## 6. Startup assertion

In `status.py`:

```python
_ENUM_LABELS_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = 'document_status' ORDER BY e.enumsortorder"
)

async def assert_status_enum_matches_db(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        labels = tuple((await conn.execute(_ENUM_LABELS_SQL)).scalars().all())
    if labels != DOCUMENT_STATUSES:
        raise RuntimeError(
            f"document_status enum drift: postgres has {labels}, "
            f"DocumentStatus has {DOCUMENT_STATUSES}"
        )
```

Compared as an **ordered tuple**, not a set. `pg_enum` ordinality is part of the type's identity, and
an ordered comparison is the same amount of code while catching strictly more drift.

Called in `app/main.py`'s `lifespan`, and unlike the existing `ping()` / Chroma probes it is
**fatal**: those are "a dependency is down, stay up so `/health/ready` can say so"; this one is "the
schema is not the schema this code was written against", and continuing means writing wrong data.
Boot fails.

Verified today against the live DB: labels are `queued, parsing, indexing, ready, failed,
quarantined` in ordinals 1–6. The assertion passes as-is.

> **Decision D4.** `app/cli.py` builds its own engine and never runs `lifespan`, so the CLI would not
> get this check. One line in the CLI's bootstrap covers it. Recommend yes.

---

## 7. Migrating the existing writes

All ten current writes are ORM attribute assignment; there is no raw `UPDATE documents` anywhere.
Three of them conflict with your table, and the conflicts are the substance of this section.

### 7.1 `cli.py:221` — reindex does `ready → queued`, which your table forbids

Your table reaches `queued` only from `failed`. The fix is not to widen the table — it is that the
CLI should not be driving the state machine at all. `_cmd_reindex` stops writing `status`, sets
`document.error = None`, and calls `ingest_document(..., force=True)`. A `ready` document then takes
the table's own `ready → parsing` edge and never passes through `queued`.

`force: bool = False` is a new keyword on `ingest_document()` whose only job is to bypass the
`ready → no-op` dedupe shortcut — which is precisely the comment already sitting at `cli.py:220`
("Bypass ingest_document()'s ready -> no-op dedupe shortcut on purpose"), moved from a hand-rolled
status poke into a parameter.

### 7.2 `ingestion.py:118` — re-ingest does `quarantined → queued`, which your table forbids

`quarantined` is terminal, so this path must stop existing. `ingest_document()` gains an early return
for it, mirroring the `ready` dedupe shortcut: return the document unchanged. Quarantine is a
legitimate final answer about a document, not an error in the caller, and returning it unchanged makes
the CLI exit `EXIT_PIPELINE_FAILED` (via `TERMINAL_FAILURE_STATUSES`) with no code change — which is
already the behaviour today.

**This is a real behaviour change:** re-ingesting a quarantined document currently retries it, and
after this chunk it will not. That is what "quarantined → {} (only true terminal state)" means, and
it is the correct reading of your table, but it is worth naming out loud.

### 7.3 Documents stranded in `parsing` / `indexing` by a crashed run

Today, line 118 forces any non-`ready` document straight back to `queued`, which silently covers this
case. Your table has no `parsing → parsing`, no `indexing → parsing` and no direct route back to
`queued` from either — so the naive migration would strand every crash-interrupted document forever.

The table already contains the recovery path, it just has to be walked explicitly:
`parsing → failed` and `indexing → failed` are both legal, and `failed → queued` is legal. So
`ingest_document()` sweeps a stranded document to `failed` with
`reason="abandoned by an interrupted run"`, then retries it from `queued`. Two extra calls, and the
abandonment is recorded in the audit trail instead of being erased.

**No change to `LEGAL_TRANSITIONS` is needed.** The table as you wrote it is self-consistent.

### 7.4 Resulting preamble to `ingest_document()`

```
READY and not force      -> return unchanged            (dedupe, unchanged behaviour)
QUARANTINED              -> return unchanged            (NEW: terminal)
PARSING | INDEXING       -> transition(FAILED, reason="abandoned by an interrupted run")
                            then fall through to the FAILED branch
FAILED                   -> transition(QUEUED, reason="retry")
QUEUED                   -> nothing
READY and force          -> nothing
--- then, for every surviving path ---
transition(PARSING)      -- legal from both QUEUED and READY
```

### 7.5 Site-by-site

| Site | Today | After |
|---|---|---|
| `ingestion.py:118` | `= "queued"` (from failed **or quarantined**) | preamble above; quarantined returns early |
| `ingestion.py:127` | `status="queued"` at construction | unchanged — an INSERT, not a transition |
| `ingestion.py:136` | `= "parsing"` | `transition(session, id, PARSING)` |
| `ingestion.py:141` | `= "quarantined"` / `"failed"` | `transition(..., QUARANTINED if UNSAFE_STRUCTURE else FAILED, reason=exc.message)` |
| `ingestion.py:149` | `= "indexing"` | `transition(session, id, INDEXING)` |
| `ingestion.py:154` | `= "failed"` (no chunks) | `transition(..., FAILED, reason="no extractable content")` |
| `ingestion.py:162` | `= "failed"` (token limit) | `transition(..., FAILED, reason="exceeds indexing size limit")` |
| `ingestion.py:193` | `= "failed"` (embedding) | `transition(..., FAILED, reason="embedding service unavailable")` |
| `ingestion.py:233` | `= "ready"` | `transition(session, id, READY)` |
| `cli.py:221` | `= "queued"` | **deleted** — `force=True` instead |

Non-status assignments beside these (`document.error`, `chunk_count`, `indexed_at`, `page_count`,
`mime_type`) stay exactly where they are, set on the `Document` the caller already holds — which is
the same identity-mapped instance `transition()` returns, per §4.

After this, the only place in the codebase that writes `documents.status` on an existing row is
`app/documents/status.py`. That is checkable, and §8 checks it.

---

## 8. Tests

**Convention: `_demo()` under `__main__`, not pytest.** `tests/` currently holds only `fixtures/`;
every check in this repo is an in-module `_demo()` with asserts. `pytest` is not in `pyproject.toml`
and not installed, and installing it is approval gate #6. Step 06 is "Tests" and will introduce a real
suite for the whole repo — introducing it here for one module fragments the convention for nicer
failure output, not for correctness. **Recommend: `_demo()` now, port to pytest at Step 06.**
Say the word if you want pytest now instead.

`_demo()` follows `ingestion.py`'s established shape: create a throwaway `User` against the dev
Postgres, `try/finally`, `DELETE FROM users WHERE id = :id` + commit at the end (documents CASCADE).
It additionally deletes the `audit_events` rows by `resource_id`, since those are `ON DELETE SET NULL`
and would otherwise be left orphaned in the dev DB.

### 8.1 Full product of (from, to)

```python
for frm, to in itertools.product(DocumentStatus, repeat=2):
    await _seed_status(session, doc_id, frm)      # raw UPDATE — the one deliberate bypass
    expected_ok = to in LEGAL_TRANSITIONS[frm]
    ...assert success or IllegalStatusTransition accordingly
```

36 pairs, no hand-written case list. The expectation is read from `LEGAL_TRANSITIONS` itself, so the
table and the test cannot drift apart. Self-transitions (the 6 diagonal pairs) are covered by the same
loop and must all raise.

Seeding uses a raw `UPDATE documents SET status = :s`, which is the only place in the repo that
bypasses `transition()` — marked with a comment saying exactly that, because it has to bypass it:
using `transition()` to set up `transition()`'s own test is circular.

On success the assertions are: the returned `Document` is the identity-mapped instance, its `.status`
equals `to`, the row on disk equals `to` after commit, and exactly one matching `audit_events` row
exists with the right `payload.from` / `payload.to`.

### 8.2 Bad IDs

```python
for bad in ("doc_DOESNOTEXIST00000000000", "", "not-an-id", "x" * 200):
    ...assert DocumentNotFound
```

Both unknown and malformed, per your spec, including an over-length string to prove the `varchar(40)`
comparison does not raise instead.

### 8.3 Concurrency — exactly one winner

Two `AsyncSession`s on two connections, both attempting `queued → parsing` on the same document:

1. A opens a transaction and calls `transition()`. Does not commit.
2. B calls `transition()` concurrently (`asyncio.gather`) and blocks on A's row lock.
3. A commits.
4. B unblocks, re-evaluates its `WHERE` against the committed row, matches zero rows, raises.

Assert: exactly one of the two returns a `Document`, exactly one raises
`IllegalStatusTransition`, and `audit_events` holds exactly **one** row for that transition — the
loser must not have logged a state change it did not make.

Uses `asyncio.wait_for` with a timeout so a deadlock or a mis-scoped transaction fails the check
instead of hanging it.

### 8.4 Guard against regression

One grep-style assertion in `_demo()`: no file under `app/` outside `app/documents/status.py`
contains a `.status = ` assignment on a `Document`. Cheap, and it is the thing that actually decays.

---

## 9. Decisions I need from you

| # | Decision | My recommendation |
|---|---|---|
| D1 | `app/documents/status.py` vs `app/services/document_status.py` | Yours as written; flagged only because it adds a 7th package |
| D2 | Re-ingesting a `quarantined` document stops retrying (§7.2) | Correct per your table — confirming because it changes behaviour |
| D3 | `reason` → `audit_events` payload, absorbing 5 `_audit()` calls (§5) | Yes — otherwise `reason` is dead |
| D4 | Run the enum assertion in the CLI bootstrap too (§6) | Yes, one line |
| D5 | `_demo()` self-check now vs pytest now (§8) | `_demo()` now, pytest at Step 06 |

---

## 10. Explicitly out of scope for this chunk

- **`status_changed_at` and its migration** — §0. Add it when a stale-document sweeper exists.
- **Retrieval filtering on `status = 'ready'`** — neither retrieval arm filters on document status
  today (`_LEXICAL_SQL` never joins `documents`; the vector arm filters on `document_id` only). Your
  new `ready → parsing` edge means a document being re-parsed keeps its old chunks and stays
  retrievable mid-reparse. Real, but it is a retrieval change, not a status-module change. Flagging
  it for its own chunk rather than smuggling it in here.
- **Idempotent / no-op transitions.** No caller needs one. Not adding an opt-in flag for a
  hypothetical.
- **Typing the column `Mapped[DocumentStatus]`** — §2.
