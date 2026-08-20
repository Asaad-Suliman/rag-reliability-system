"""The only place in the codebase that writes `documents.status` on an existing row.

Legality is enforced by the database, not by Python. `transition()` issues one
conditional `UPDATE ... WHERE status IN (legal predecessors) ... RETURNING`, so
the check and the write are the same statement: two workers racing the same
transition cannot both pass a Python-side test and clobber each other, because
the loser's `WHERE` is re-evaluated against the winner's committed row and
matches nothing.

The disambiguating `SELECT` — "was that zero rows because the document is gone,
or because the move was illegal?" — runs only on the failure path. The happy
path is one round trip.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import DOCUMENT_STATUSES, AuditEvent, Document, DocumentStatus

logger = logging.getLogger(__name__)

_Q = DocumentStatus

# Written once. `quarantined` is the only true terminal state: a document whose
# structure was judged unsafe is a final answer about that document, not a step
# on the way to one.
#
# Note what is deliberately absent: there is no `parsing -> parsing` or
# `indexing -> parsing` edge, so a document stranded by a crashed run cannot be
# silently re-driven. The recovery route is the explicit one the table already
# contains — sweep it to `failed` (legal from both), then retry from `queued` —
# which leaves the abandonment in the audit trail instead of erasing it.
LEGAL_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    _Q.QUEUED: frozenset({_Q.PARSING, _Q.QUARANTINED, _Q.FAILED}),
    _Q.PARSING: frozenset({_Q.INDEXING, _Q.QUARANTINED, _Q.FAILED}),
    _Q.INDEXING: frozenset({_Q.READY, _Q.FAILED}),
    _Q.READY: frozenset({_Q.PARSING}),
    _Q.FAILED: frozenset({_Q.QUEUED}),
    _Q.QUARANTINED: frozenset(),
}

# Inverted at import time rather than hand-written a second time: a table that
# has to be edited in two places is a table that will eventually disagree with
# itself. Self-transitions are illegal for free — no state lists itself above,
# so no state appears in its own predecessor set.
LEGAL_PREDECESSORS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    to: frozenset(frm for frm, tos in LEGAL_TRANSITIONS.items() if to in tos)
    for to in DocumentStatus
}


class DocumentNotFound(LookupError):
    """No `documents` row with this id. Also the answer for a malformed id: the
    id is only ever compared, never stored, so a garbage or over-length string
    simply matches nothing.
    """

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(f"no document with id {document_id!r}")


class IllegalStatusTransition(ValueError):
    """The document exists but is not in a state from which `to_status` is
    reachable. `from_status`/`to_status` are attributes, not just message text,
    so Step 04 can map them onto contract error codes without parsing strings.
    """

    def __init__(
        self, document_id: str, from_status: DocumentStatus, to_status: DocumentStatus
    ) -> None:
        self.document_id = document_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"{document_id}: {from_status.value} -> {to_status.value} is not a legal transition "
            f"(legal from {from_status.value}: "
            f"{', '.join(sorted(s.value for s in LEGAL_TRANSITIONS[from_status])) or 'nothing'})"
        )


async def transition(
    session: AsyncSession,
    document_id: str,
    to: DocumentStatus,
    *,
    reason: str | None = None,
) -> Document:
    """Move a document to `to`, or raise. Returns the updated `Document`.

    Never commits: the caller owns the transaction boundary. Records the move in
    `audit_events` with `reason` in the payload — the insert is buffered in the
    unit of work and flushed by the caller's existing commit, so it costs no
    extra round trip.

    Raises `DocumentNotFound` if no such document, `IllegalStatusTransition` if
    it exists but is not in a legal predecessor state (self-transitions
    included).
    """
    stmt = (
        update(Document)
        .where(Document.id == document_id, Document.status.in_(LEGAL_PREDECESSORS[to]))
        .values(status=to)
        .returning(Document)
        # Not cosmetic: callers hold a live `Document` in the session's identity
        # map, and without this the returned object can still carry the stale
        # in-memory status. On Postgres SQLAlchemy satisfies the resync from
        # RETURNING rather than a pre-SELECT, so this stays one round trip.
        .execution_options(synchronize_session="fetch")
    )
    document = (await session.execute(stmt)).scalars().one_or_none()

    if document is None:
        # Failure path only. Racy by construction — a third party can change or
        # delete the row between the two statements — so `from_status` is a
        # best-effort diagnostic. The verdict it qualifies is never wrong: the
        # transition did not happen.
        current = (
            await session.execute(select(Document.status).where(Document.id == document_id))
        ).scalar_one_or_none()
        if current is None:
            raise DocumentNotFound(document_id)
        raise IllegalStatusTransition(document_id, DocumentStatus(current), to)

    # ponytail: no `from` in the payload. Postgres 16's RETURNING sees only the
    # new row, and reading the old one first would reintroduce exactly the
    # read-then-write this function exists to avoid. The predecessor is
    # recoverable from the preceding audit row for the same resource_id; if that
    # ever stops being good enough, the upgrade is a CTE capturing the old value
    # in the same statement, not a second query.
    session.add(
        AuditEvent(
            user_id=document.user_id,
            event_type=f"document_status_{to.value}",
            resource_type="document",
            resource_id=document.id,
            payload={"to": to.value, "reason": reason},
        )
    )
    logger.info(
        "document status transition",
        extra={"document_id": document.id, "to": to.value, "reason": reason},
    )
    return document


_ENUM_LABELS_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = 'document_status' ORDER BY e.enumsortorder"
)


async def assert_status_enum_matches_db(engine: AsyncEngine) -> None:
    """Fail boot if the live `document_status` type is not what this code
    believes it is.

    Fatal, unlike the reachability probes next to it at startup: those mean "a
    dependency is down, stay up so /health/ready can report it", this one means
    "the schema is not the schema this code was written against", and carrying
    on writes wrong data. Compared as an ordered tuple, not a set — `pg_enum`
    ordinality is part of the type's identity, and ordering the comparison
    catches strictly more drift for the same line of code.
    """
    async with engine.connect() as conn:
        labels = tuple((await conn.execute(_ENUM_LABELS_SQL)).scalars().all())
    if not labels:
        raise RuntimeError(
            "postgres has no `document_status` enum type — is migration 0002 applied?"
        )
    if labels != DOCUMENT_STATUSES:
        raise RuntimeError(
            f"document_status enum drift: postgres has {labels}, "
            f"DocumentStatus has {DOCUMENT_STATUSES}"
        )


def _demo() -> None:
    """Self-check against the dev database. `uv run python -m app.documents.status`.

    Follows this repo's convention (see `app/services/ingestion.py`): a throwaway
    `User` in the live dev Postgres, `try/finally`, deleted at the end. Documents
    CASCADE with the user; `audit_events.user_id` is ON DELETE SET NULL, so those
    rows are cleaned up explicitly by `resource_id`.
    """
    import asyncio
    import itertools
    from pathlib import Path

    from sqlalchemy import delete

    from app.core.config import get_settings
    from app.db.models import User
    from app.db.session import create_engine, create_session_factory

    # --- pure table checks, no I/O ---
    # The inverse really is the inverse: every edge appears in exactly one
    # predecessor set, and no state is its own predecessor.
    edges = {(frm, to) for frm, tos in LEGAL_TRANSITIONS.items() for to in tos}
    inverse_edges = {(frm, to) for to, frms in LEGAL_PREDECESSORS.items() for frm in frms}
    assert edges == inverse_edges, edges ^ inverse_edges
    assert all(s not in LEGAL_PREDECESSORS[s] for s in DocumentStatus), "self-transition leaked in"
    assert LEGAL_TRANSITIONS[_Q.QUARANTINED] == frozenset(), "quarantined must be terminal"
    assert set(LEGAL_TRANSITIONS) == set(DocumentStatus), "table must cover every status"

    async def run() -> None:
        settings = get_settings()
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)

        await assert_status_enum_matches_db(engine)

        async def seed(session: AsyncSession, document_id: str, status: DocumentStatus) -> None:
            """The one deliberate bypass of `transition()` in the whole repo.

            It has to bypass: using `transition()` to set up `transition()`'s own
            test would be circular, and several start states are unreachable in
            one legal hop anyway.
            """
            await session.execute(
                text("UPDATE documents SET status = CAST(:s AS document_status) WHERE id = :id"),
                {"s": status.value, "id": document_id},
            )
            await session.commit()
            session.expire_all()

        async with session_factory() as session:
            user = User(email="status-selfcheck@test.local", password_hash="x")
            session.add(user)
            await session.flush()
            user_id = user.id

            document = Document(
                user_id=user_id,
                filename="status-selfcheck.txt",
                mime_type="text/plain",
                size_bytes=1,
                sha256="0" * 64,
                status=DocumentStatus.QUEUED,
                chunk_count=0,
            )
            session.add(document)
            await session.flush()
            document_id = document.id
            await session.commit()

            try:
                # --- 1. the full 6x6 product, expectations read from the table ---
                legal = illegal = 0
                for frm, to in itertools.product(DocumentStatus, repeat=2):
                    await seed(session, document_id, frm)
                    expected_ok = to in LEGAL_TRANSITIONS[frm]
                    try:
                        returned = await transition(
                            session, document_id, to, reason=f"{frm.value}->{to.value}"
                        )
                    except IllegalStatusTransition as exc:
                        assert not expected_ok, f"{frm.value} -> {to.value} should have succeeded"
                        assert exc.from_status is frm, (exc.from_status, frm)
                        assert exc.to_status is to, (exc.to_status, to)
                        await session.rollback()
                        illegal += 1
                        continue

                    assert expected_ok, f"{frm.value} -> {to.value} should have been rejected"
                    # The caller's own instance, resynced -- not a detached copy.
                    assert returned is document, "must return the identity-mapped instance"
                    assert returned.status == to, (returned.status, to)
                    await session.commit()
                    on_disk = (
                        await session.execute(
                            text("SELECT status FROM documents WHERE id = :id"), {"id": document_id}
                        )
                    ).scalar_one()
                    assert on_disk == to.value, (on_disk, to.value)
                    legal += 1

                assert legal + illegal == 36, (legal, illegal)
                assert legal == sum(len(t) for t in LEGAL_TRANSITIONS.values()) == 10, legal

                # Every legal move left exactly one audit row carrying its reason.
                audit_count = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM audit_events WHERE resource_id = :id "
                            "AND event_type LIKE 'document_status_%'"
                        ),
                        {"id": document_id},
                    )
                ).scalar_one()
                assert audit_count == legal, (audit_count, legal)

                # --- 2. unknown and malformed ids both raise DocumentNotFound ---
                for bad in ("doc_00000000000000000000000000", "", "not-an-id", "x" * 200):
                    raised = False
                    try:
                        await transition(session, bad, DocumentStatus.PARSING)
                    except DocumentNotFound:
                        raised = True
                    await session.rollback()
                    assert raised, f"{bad!r} should raise DocumentNotFound"

                # --- 3. concurrency: two sessions, one winner ---
                await seed(session, document_id, DocumentStatus.QUEUED)

                started = asyncio.Event()

                async def contender(hold: bool) -> str:
                    async with session_factory() as own:
                        try:
                            if hold:
                                await transition(own, document_id, DocumentStatus.PARSING)
                                started.set()
                                # Hold the row lock long enough that the other
                                # contender is provably blocked on it, not merely
                                # scheduled after it.
                                await asyncio.sleep(0.25)
                                await own.commit()
                            else:
                                await started.wait()
                                await transition(own, document_id, DocumentStatus.PARSING)
                                await own.commit()
                        except IllegalStatusTransition:
                            await own.rollback()
                            return "lost"
                        return "won"

                outcomes = await asyncio.wait_for(
                    asyncio.gather(contender(hold=True), contender(hold=False)), timeout=15
                )
                assert sorted(outcomes) == ["lost", "won"], outcomes

                # The loser must not have logged a transition it never made.
                race_rows = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM audit_events WHERE resource_id = :id "
                            "AND event_type = 'document_status_parsing' "
                            "AND payload->>'reason' IS NULL"
                        ),
                        {"id": document_id},
                    )
                ).scalar_one()
                assert race_rows == 1, race_rows

                # --- 4. nothing outside this module writes Document.status ---
                app_root = Path(__file__).resolve().parent.parent
                offenders = [
                    f"{path.relative_to(app_root)}:{n}"
                    for path in app_root.rglob("*.py")
                    if path.resolve() != Path(__file__).resolve()
                    for n, line in enumerate(path.read_text().splitlines(), start=1)
                    if ".status = " in line and "document" in line.lower()
                ]
                assert not offenders, f"status written outside status.py: {offenders}"
            finally:
                await session.rollback()
                await session.execute(
                    text("DELETE FROM audit_events WHERE resource_id = :id"), {"id": document_id}
                )
                await session.execute(delete(User).where(User.id == user_id))
                await session.commit()

        await engine.dispose()
        print(
            f"ok: {legal} legal / {illegal} illegal transitions over the full 6x6 product, "
            "bad ids, one-winner concurrency, and single-writer containment all verified"
        )

    asyncio.run(run())


if __name__ == "__main__":
    _demo()
