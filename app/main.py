"""Application entry point: `uvicorn app.main:app`."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, SettingsError, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.log_level)

    app = FastAPI(
        title="RAG Reliability System",
        version="0.1.0",
        # Do not publish the schema in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)

    logger.info("application configured", extra={"app_env": settings.app_env})
    return app


try:
    app = create_app(get_settings())
except SettingsError as exc:
    # A missing secret is a startup failure, not a stack trace.
    raise SystemExit(f"startup aborted: {exc}") from exc
