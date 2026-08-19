"""`messages` — API Contract §5.1.

`citations`, `trust`, `guardrail`, `plan`, and `timings_ms` are JSONB rather than
five relational tables: the contract already defines their shape as nested JSON,
nothing in the app queries inside them, and Step 03/04 own populating them.
Splitting them into tables now would be schema speculating ahead of the agents
that produce the data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.ids import new_id


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # answered | refused_ungrounded | refused_blocked | refused_no_context | failed
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    trust: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    guardrail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    timings_ms: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
