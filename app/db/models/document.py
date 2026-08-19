"""`documents` — API Contract §3.

`status` is a native Postgres enum holding exactly the contract's six values.
`UNIQUE (user_id, sha256)` is the dedupe mechanism: re-uploading the same bytes
hits this constraint and maps to the contract's `CONFLICT` (409), so dedupe is
enforced by the database, not by an application-level pre-check that could race.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.ids import new_id

DOCUMENT_STATUSES = ("queued", "parsing", "indexing", "ready", "failed", "quarantined")

DocumentStatusType = PGEnum(*DOCUMENT_STATUSES, name="document_status", create_type=True)


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
    status: Mapped[str] = mapped_column(DocumentStatusType, nullable=False, default="queued")
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # User-safe reason only — never a traceback. Set on failed/quarantined.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
