"""Declarative base and the metadata Alembic autogenerates against."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Parent of every ORM model."""


# Importing model modules here registers their tables on Base.metadata.
# The import is for its side effect (table registration on Base.metadata),
# not for the names — Alembic autogenerate and CLI code that instantiates
# models import from app.db.models directly.
from app.db import models as models  # noqa: E402,F401

metadata = Base.metadata
