"""Application entry point: `uvicorn app.main:app`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.health import CHECK_TIMEOUT_SECONDS
from app.api.v1.router import api_router
from app.core.config import Settings, SettingsError, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware
from app.db.session import create_engine, create_session_factory, ping
from app.documents.status import assert_status_enum_matches_db
from app.services.embeddings import VoyageEmbedder
from app.services.reranking import RerankerModelMissing, build_reranker
from app.services.vector_store import CORPUS_DIR, ExactVectorStore

logger = logging.getLogger(__name__)

STARTUP_PROBE_TIMEOUT_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.vector_store = ExactVectorStore()

    # Same rule as the reranker below: built ONCE here and passed into
    # `retrieve()` as a parameter. Construction opens no connection and costs
    # nothing — VoyageEmbedder only reaches the network inside `embed()`.
    app.state.embedder = VoyageEmbedder(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.voyage_model,
        dimensions=settings.voyage_dimensions,
    )

    # Loaded ONCE, here, and injected into `retrieve()` as a parameter — never
    # per request, and never imported as a module global. An InferenceSession
    # costs a few hundred ms to build and holds 22 MiB; building one per request
    # would be the single worst thing in the query path.
    #
    # Fatal on failure, unlike the Postgres/vector probes below. Those stay
    # non-fatal so /health/ready can *report* a dependency being down — they are
    # external and transient. A missing or mismatched weight file is neither: it
    # is a build defect that will never fix itself, and the alternative is
    # serving answers that claim to be reranked and are not. Same reasoning as
    # the `document_status` enum check further down.
    app.state.reranker = build_reranker(
        settings.reranker,
        model_dir=settings.reranker_model_dir,
        manifest_path=settings.reranker_manifest_path,
        intra_op_threads=settings.reranker_intra_op_threads,
    )
    logger.info(
        "reranker loaded",
        extra={
            "backend": settings.reranker,
            "model_dir": str(settings.reranker_model_dir),
            "intra_op_threads": settings.reranker_intra_op_threads,
        },
    )

    # Probe both dependencies, but never abort startup over them: /health/ready
    # has to stay reachable to *report* a dependency being down.
    postgres_reachable = False
    try:
        await ping(app.state.engine, STARTUP_PROBE_TIMEOUT_SECONDS)
        postgres_reachable = True
        logger.info("postgres reachable", extra={"pool_size": settings.db_pool_size})
    except Exception as exc:
        logger.error(
            "postgres unreachable at startup — /health/ready will report 503",
            extra={"reason": str(exc)},
        )

    # Fatal, unlike the probes around it. Those mean "a dependency is down, stay
    # up so /health/ready can say so"; this means "the schema is not the schema
    # this code was written against", and serving on would write wrong data.
    # Gated on reachability so an unreachable database still yields a live app
    # that can report 503, rather than a connection error dressed up as drift.
    if postgres_reachable:
        await assert_status_enum_matches_db(app.state.engine)

    try:
        await app.state.vector_store.check(STARTUP_PROBE_TIMEOUT_SECONDS)
        logger.info("vector corpus loaded", extra={"corpus_dir": str(CORPUS_DIR)})
    except Exception as exc:
        logger.error(
            "vector corpus unavailable at startup — /health/ready will report 503",
            extra={"reason": str(exc)},
        )

    yield

    await app.state.engine.dispose()
    app.state.vector_store.close()
    app.state.reranker.close()
    logger.info("shutdown complete")


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.log_level)

    app = FastAPI(
        title="RAG Reliability System",
        version="0.1.0",
        lifespan=lifespan,
        # Do not publish the schema in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)

    logger.info(
        "application configured",
        extra={"app_env": settings.app_env, "check_timeout_s": CHECK_TIMEOUT_SECONDS},
    )
    return app


try:
    app = create_app(get_settings())
except SettingsError as exc:
    # A missing secret is a startup failure, not a stack trace.
    raise SystemExit(f"startup aborted: {exc}") from exc
except RerankerModelMissing as exc:
    # Raised from lifespan, so in practice uvicorn surfaces it at first request;
    # caught here too for the case where the app is constructed directly.
    raise SystemExit(f"startup aborted: {exc}") from exc
