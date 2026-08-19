"""`chunks` — provenance is not optional.

`document_id`, `page`, `char_start`, `char_end` are all `NOT NULL`: Step 03's
Verifier scores groundedness against these spans, so the guarantee lives in the
DDL, not in application discipline. `content_tsv` is a generated column so
Postgres keeps it in sync with `text` automatically; it backs the keyword half
of hybrid retrieval via a GIN index — see migration `0002`.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.ids import new_id


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("chk"))
    document_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Position within the document — 0-based, stable across reindex.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Character-based estimate, not a real tokenizer count — see chunking.py.
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    # Null until the embedding step upserts this chunk into the vector store.
    vector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # content_tsv (generated tsvector + GIN index) is added in migration 0002
    # directly via raw DDL — SQLAlchemy's Computed() support for reflection
    # back into the ORM model is unreliable across dialects, and nothing in
    # the app ever writes to this column, so it is DB-managed and not mapped
    # here.
