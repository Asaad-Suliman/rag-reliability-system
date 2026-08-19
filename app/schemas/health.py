"""Health payloads — API Contract §6."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """Liveness: the process is up. No dependencies are consulted."""

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """Readiness: identical shape on 200 and 503 — only the values differ."""

    status: CheckStatus
    checks: dict[str, CheckStatus]
