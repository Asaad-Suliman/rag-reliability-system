"""Request-scoped context: one id per request, on every response and every log line."""

from __future__ import annotations

import re
from contextvars import ContextVar
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
NO_REQUEST_ID = "-"

_MAX_INBOUND_LENGTH = 64
# Inbound ids reach logs and response headers, so accept only an opaque token shape.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_request_id: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST_ID)


def get_request_id() -> str:
    """The current request's id, or `-` outside a request (startup, shutdown, workers)."""
    return _request_id.get()


def new_request_id() -> str:
    return f"req_{uuid4().hex[:12]}"


def resolve_request_id(inbound: str | None) -> str:
    """Reuse a caller-supplied id only if it is a safe, bounded token."""
    if inbound is not None and len(inbound) <= _MAX_INBOUND_LENGTH:
        candidate = inbound.strip()
        if _SAFE_REQUEST_ID.match(candidate):
            return candidate
    return new_request_id()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assign a request id, expose it to logs and handlers, echo it on every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        # request.state survives past the contextvar reset below, which is what the
        # 500 handler reads: it runs outside this middleware, in ServerErrorMiddleware.
        request.state.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
