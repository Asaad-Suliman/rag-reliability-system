"""Health endpoints — API Contract §6."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, Request, Response

from app.core.config import Settings
from app.db.session import ping
from app.schemas.health import CheckStatus, HealthResponse, ReadinessResponse
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Readiness answers a load balancer, so it must fail fast rather than hang.
CHECK_TIMEOUT_SECONDS = 2.0

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    """Is the process alive? Deliberately checks nothing else."""
    return HealthResponse()


async def _run_check(name: str, probe: Awaitable[None]) -> CheckStatus:
    """Never let a failing dependency raise out of the readiness endpoint."""
    try:
        await probe
    except TimeoutError:
        logger.warning("readiness check timed out", extra={"check": name})
        return "error"
    except Exception as exc:
        logger.warning("readiness check failed", extra={"check": name, "reason": str(exc)})
        return "error"
    return "ok"


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness")
async def ready(request: Request, response: Response) -> ReadinessResponse:
    """Report every dependency. Same JSON shape whether the answer is 200 or 503."""
    settings: Settings = request.app.state.settings
    store: VectorStore = request.app.state.vector_store

    postgres, vectors = await asyncio.gather(
        _run_check("postgres", ping(request.app.state.engine, CHECK_TIMEOUT_SECONDS)),
        _run_check("vectors", store.check(CHECK_TIMEOUT_SECONDS)),
    )
    # Configuration presence only — calling the provider costs money. Step 02
    # replaces this with a real upstream probe.
    llm: CheckStatus = "ok" if settings.llm_api_key.get_secret_value().strip() else "error"

    checks: dict[str, CheckStatus] = {"postgres": postgres, "vectors": vectors, "llm": llm}
    healthy = all(status == "ok" for status in checks.values())
    response.status_code = 200 if healthy else 503
    return ReadinessResponse(status="ok" if healthy else "error", checks=checks)
