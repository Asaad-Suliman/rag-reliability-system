"""Async engine, session factory, and the liveness probe used by /health/ready.

Pool sizing is explicit on purpose: SQLAlchemy's defaults (pool_size=5,
max_overflow=10) are a silent capacity decision, and capacity decisions belong
in configuration where they can be reasoned about and tuned per environment.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

logger = logging.getLogger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        # Recycle dead connections instead of handing them to a request.
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine, timeout: float) -> None:
    """Raise if Postgres cannot answer a trivial query inside `timeout` seconds."""
    async with asyncio.timeout(timeout):
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
