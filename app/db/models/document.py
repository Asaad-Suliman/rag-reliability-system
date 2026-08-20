"""`documents` — API Contract §3.

`status` is a native Postgres enum holding exactly the contract's six values.
`UNIQUE (user_id, sha256)` is the dedupe mechanism: re-uploading the same bytes
hits this constraint and maps to the contract's `CONFLICT` (409), so dedupe is
enforced by the database, not by an application-level pre-check that could race.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.ids import new_id


class DocumentStatus(StrEnum):
    """The contract's six statuses, as the single source of truth.

    `StrEnum`, not `Enum`: members compare equal to their own string values, so
    `document.status == DocumentStatus.READY` and the pre-existing
    `document.status == "ready"` are the same test, and assigning a member to
    the `Mapped[str]` column writes the plain label. Legal movement between
    these values lives in `app/documents/status.py`, which is the only place
    that may write this column on an existing row.
    """

    QUEUED = "queued"
    PARSING = "parsing"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


# Derived, never hand-written twice. Order is significant: it is compared
# against `pg_enum`'s ordinals at startup by `assert_status_enum_matches_db()`.
DOCUMENT_STATUSES = tuple(status.value for status in DocumentStatus)

# `create_type=False`: migration 0002 already created this type (via its own
# inline `postgresql.ENUM` literal, independent of this object), and nothing in
# the repo calls `metadata.create_all()` — the schema only ever comes from
# migrations. Leaving it True makes Alembic autogenerate emit a `CREATE TYPE`
# for a type that already exists.
DocumentStatusType = PGEnum(*DOCUMENT_STATUSES, name="document_status", create_type=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uq_documents_user_sha256"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("doc"))
    user_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 hex digest — the content-hash dedupe key.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        DocumentStatusType, nullable=False, default=DocumentStatus.QUEUED
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # User-safe reason only — never a traceback. Set on failed/quarantined.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
