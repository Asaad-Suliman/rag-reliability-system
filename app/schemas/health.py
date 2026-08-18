"""Health payloads — API Contract §6."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness: the process is up. No dependencies are consulted."""

    status: Literal["ok"] = "ok"
