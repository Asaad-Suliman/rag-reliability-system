"""Health endpoints — API Contract §6."""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.health import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    """Is the process alive? Deliberately checks nothing else."""
    return HealthResponse()
