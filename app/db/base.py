"""Declarative base and the metadata Alembic autogenerates against."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Parent of every ORM model."""


# Importing model modules here registers their tables on Base.metadata.
# Nothing to import yet — the first models arrive in Step 04.
metadata = Base.metadata
