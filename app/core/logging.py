"""Structured JSON logging with secret redaction.

Stdlib only: a formatter plus a handful of regexes does the whole job, and the
redaction runs on the final rendered text so it also covers values that arrived
through `%s` args or an exception message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.middleware import get_request_id

# Attributes LogRecord always carries; anything else was passed via `extra=`.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "color_message",  # uvicorn duplicates the message with ANSI codes
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_REDACTED = "***"

# key=value / "key": "value" for anything that smells like a credential
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|dsn)\b"
    r"(\s*[=:]\s*)"
    r"(\"?)([^\"\s,;}]+)\3"
)
# postgresql+asyncpg://user:password@host/db
_DSN_PASSWORD = re.compile(r"(?i)(://[^:/@\s]+:)([^@\s]+)(@)")
# Authorization: Bearer eyJ...
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")
# "Authorization: Bearer <tok>" hits two rules in a row and leaves "*** ***".
_REDACTION_RUN = re.compile(r"\*\*\*(?:\s+\*\*\*)+")


def redact(text: str) -> str:
    """Strip credential-shaped substrings. Cheap, conservative, never raises.

    Order matters: Bearer runs before the key=value rule, otherwise the latter
    masks the word "Bearer" and leaves the token itself in the clear.
    """
    text = _DSN_PASSWORD.sub(rf"\1{_REDACTED}\3", text)
    text = _BEARER_TOKEN.sub(f"Bearer {_REDACTED}", text)
    text = _SECRET_ASSIGNMENT.sub(rf"\1\2\3{_REDACTED}\3", text)
    return _REDACTION_RUN.sub(_REDACTED, text)


def scrub(value: Any) -> Any:
    """Redact every string in a log payload *before* serialization.

    Redacting the serialized JSON instead would be wrong: json.dumps escapes the
    quotes in `api_key="secret"`, and the escaped form slips past the regex.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact(str(value))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, always carrying the current request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": get_request_id(),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(scrub(payload), ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Route every logger — ours and uvicorn's — through one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; drop them so nothing bypasses the redaction.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def configure_cli_logging(level: str) -> None:
    """Same JSON formatting and secret redaction as `configure_logging`, but
    routed to stderr instead of stdout.

    A separate function, not a `stream=` parameter on `configure_logging`,
    so `main.py`'s call site and behavior are completely untouched — the CLI
    reserves stdout for command results (so e.g. `query` output stays
    pipeable) and never calls `configure_logging` itself. No uvicorn handler
    cleanup here: the CLI process never runs uvicorn.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
