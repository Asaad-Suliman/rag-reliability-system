"""The single error shape defined by API Contract §1.1.

Every 4xx and 5xx leaves the app as
`{"error": {"code", "message", "request_id", "details"}}` — never a traceback,
never a raw FastAPI validation dump.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.middleware import REQUEST_ID_HEADER, get_request_id
from app.services.generation import (
    GenerationFailed,
    GenerationUpstreamError,
    ProviderRequestError,
)
from app.services.retrieval import FarFieldInputError
from app.services.vector_store import CorpusUnavailableError

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
}


def code_for_status(status_code: int) -> ErrorCode:
    """Map an HTTP status onto a contract code.

    Unmapped 4xx (405, 406, ...) become VALIDATION_ERROR: the contract defines no
    generic client-error code, and inventing one is a breaking contract change.
    """
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    return ErrorCode.VALIDATION_ERROR if status_code < 500 else ErrorCode.INTERNAL_ERROR


class AppError(Exception):
    """Raise this anywhere to produce a contract-shaped error response."""

    def __init__(
        self,
        code: ErrorCode,
        status_code: int,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        # Some refusals are only actionable WITH a header: a 429 without
        # `Retry-After` tells a client to back off for an unknown amount of time,
        # which in practice means retrying immediately. Added in chunk 8.10.
        self.headers = headers


def request_id_of(request: Request) -> str:
    """Prefer request.state — the 500 handler runs after the contextvar is reset."""
    state_id: str | None = getattr(request.state, "request_id", None)
    return state_id or get_request_id()


def error_response(
    request: Request,
    *,
    code: ErrorCode,
    status_code: int,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = request_id_of(request)
    response_headers = {REQUEST_ID_HEADER: request_id}
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status_code,
        headers=response_headers,
        content={
            "error": {
                "code": code.value,
                "message": message,
                "request_id": request_id,
                "details": details or {},
            }
        },
    )


async def app_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, AppError):  # pragma: no cover - registration guarantees the type
        return await unhandled_exception_handler(request, exc)
    return error_response(
        request,
        code=exc.code,
        status_code=exc.status_code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        return await unhandled_exception_handler(request, exc)
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0])
        fields[location] = str(error["msg"])
    return error_response(
        request,
        code=ErrorCode.VALIDATION_ERROR,
        status_code=422,
        message="Request validation failed.",
        details={"fields": fields},
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        return await unhandled_exception_handler(request, exc)
    headers = dict(exc.headers) if exc.headers else None
    return error_response(
        request,
        code=code_for_status(exc.status_code),
        status_code=exc.status_code,
        message=str(exc.detail),
        headers=headers,
    )


async def far_field_input_handler(request: Request, exc: Exception) -> Response:
    """`FarFieldInputError` -> 503 UPSTREAM_UNAVAILABLE, never a 500.

    503, not 500: nothing crashed and no code path is broken. The far-field gate was
    handed a retrieval it cannot judge — empty hits, or a top hit with no vector
    distance, which is what a degraded or unloaded vector arm produces. That is a
    dependency in a bad state, which is exactly what UPSTREAM_UNAVAILABLE and the
    readiness endpoint's own 503 already mean here, and it is retryable. A 500
    would say "this service has a bug" and bury a recoverable condition under the
    catch-all handler's opaque "An internal error occurred."

    Not a 200 with a verdict either: inventing ABSTAIN or UNVERIFIED from absent
    evidence is precisely what `FarFieldInputError` exists to prevent, and it
    would reach the client indistinguishable from a real judgement.
    """
    logger.warning(
        "far-field gate could not judge the retrieval",
        extra={"path": request.url.path, "reason": str(exc)},
    )
    return error_response(
        request,
        code=ErrorCode.UPSTREAM_UNAVAILABLE,
        status_code=503,
        message="The retrieval could not be judged. Retry shortly.",
    )


async def corpus_unavailable_handler(request: Request, exc: Exception) -> Response:
    """`CorpusUnavailableError` -> 503 UPSTREAM_UNAVAILABLE.

    It **can** reach a request: `ExactVectorStore` loads the corpus lazily
    (`_get_corpus`), and `query()` is a load site, so a missing, truncated or
    digest-mismatched artifact surfaces on the first real query rather than at
    startup — startup only *probes* it and deliberately stays up to report 503.
    That makes this reachable in exactly the state /health/ready is already
    describing as 503, so the two agree rather than contradicting each other.

    The message stays generic on purpose: the exception text names filesystem
    paths and pinned digests, which belong in the log, not in a response.
    """
    logger.error(
        "vector corpus unavailable during a request",
        extra={"path": request.url.path, "reason": str(exc)},
    )
    return error_response(
        request,
        code=ErrorCode.UPSTREAM_UNAVAILABLE,
        status_code=503,
        message="The vector corpus is unavailable.",
    )


async def generation_upstream_handler(request: Request, exc: Exception) -> Response:
    """`GenerationUpstreamError` -> 503 UPSTREAM_UNAVAILABLE (8.8 §J).

    Timeout, transport error, 429 or any 5xx from the generation provider. The
    same reasoning as `far_field_input_handler`: nothing here is broken, a
    dependency is unreachable or busy, and that is retryable. The detail
    message is deliberately distinct from the generation-failure one below so a
    client can tell "the provider was not reachable" from "the provider
    answered and produced nothing usable" without reading logs.
    """
    logger.error(
        "generation provider unavailable",
        extra={"path": request.url.path, "reason": str(exc)},
    )
    return error_response(
        request,
        code=ErrorCode.UPSTREAM_UNAVAILABLE,
        status_code=503,
        message="The answer service is unavailable. Retry shortly.",
    )


async def generation_failed_handler(request: Request, exc: Exception) -> Response:
    """`GenerationFailed` -> 503 UPSTREAM_UNAVAILABLE (8.8 §J).

    The provider answered and the answer is unusable: truncated at max_tokens,
    refused, or empty. 503 rather than 500 because no code path is broken, and
    never a 200 with a partial answer -- returning half a truncated answer as
    if it were complete is exactly the failure this system exists to not make.
    """
    logger.warning(
        "generation produced no usable answer",
        extra={"path": request.url.path, "reason": str(exc)},
    )
    return error_response(
        request,
        code=ErrorCode.UPSTREAM_UNAVAILABLE,
        status_code=503,
        message="No answer could be generated for this question. Retry shortly.",
    )


async def provider_request_handler(request: Request, exc: Exception) -> Response:
    """`ProviderRequestError` -> 500 INTERNAL_ERROR.

    A non-429 4xx from the provider -- a bad key, a bad model name, a malformed
    body. Retrying cannot fix any of those, so calling it UPSTREAM_UNAVAILABLE
    would invite a retry loop against a defect on this side. (8.8 §J does not
    cover it; decided in 8.9.)
    """
    logger.error(
        "generation provider rejected the request",
        extra={"path": request.url.path, "reason": str(exc)},
    )
    return error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        status_code=500,
        message="An internal error occurred.",
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Last resort: log the traceback server-side, return only the request id."""
    request_id = request_id_of(request)
    logger.error(
        "unhandled exception",
        # This handler runs in ServerErrorMiddleware, outside RequestIDMiddleware,
        # so the contextvar is already reset — pass the id explicitly or the one
        # log line that matters loses its correlation to the client's response.
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=exc,
    )
    return error_response(
        request,
        code=ErrorCode.INTERNAL_ERROR,
        status_code=500,
        message="An internal error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(FarFieldInputError, far_field_input_handler)
    app.add_exception_handler(CorpusUnavailableError, corpus_unavailable_handler)
    app.add_exception_handler(GenerationUpstreamError, generation_upstream_handler)
    app.add_exception_handler(GenerationFailed, generation_failed_handler)
    app.add_exception_handler(ProviderRequestError, provider_request_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
