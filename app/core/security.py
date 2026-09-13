"""The interim machine credential and the request limits that bound its spend.

**This is not Step 04.** JWT issuing/verification, password hashing and the
current-user dependency still arrive there. What lives here is one shared
MACHINE credential and two counters, added in chunk 8.10 for a single reason:
`POST /api/v1/query` calls a paid embedding API and a paid model on every
request, and until this chunk anyone who could reach the port could spend money
through it without limit.

The credential travels in `X-API-Key`, deliberately **not** in
`Authorization: Bearer` — that header is Step 04's, and keeping this path off
it means Step 04 can delete this module's auth half in one commit instead of
disentangling two schemes sharing a header. The API Contract defines no
`X-API-Key`; that divergence is recorded in `09_Memory/DECISIONS.md`.

Only the key's sha256 digest is held at runtime, and the comparison is
constant-time. Nothing here logs or echoes the presented value.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request, Response

from app.core.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)

CLIENT_KEY_HEADER = "X-API-Key"

RATELIMIT_LIMIT_HEADER = "X-RateLimit-Limit"
RATELIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
RATELIMIT_RESET_HEADER = "X-RateLimit-Reset"
RETRY_AFTER_HEADER = "Retry-After"

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_DAY = 86_400

# ONE message for both "no credential" and "wrong credential". A distinguishable
# pair tells an attacker which half of the guess was right, and there is no
# operator benefit here: the log line carries which case it was, the response
# does not.
_UNAUTHENTICATED_MESSAGE = "A valid X-API-Key header is required."

_RATE_LIMITED_MESSAGE = "Too many requests. Retry after the window resets."

# Distinct wording, same code. The contract has no budget/quota code, and adding
# one is a breaking change (API Contract, line 16), so RATE_LIMITED is reused and
# the difference is carried in the message and in `Retry-After` — which points at
# UTC midnight here, not at the end of a minute.
_DAILY_CAP_MESSAGE = "The daily request cap is exhausted. Retry after 00:00 UTC."


def key_digest(value: str) -> bytes:
    """sha256 of a credential. The only form of the key this process keeps."""
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(frozen=True)
class LimitDecision:
    """What the limiter did, in the shape the response headers need."""

    limit: int
    remaining: int
    reset: int  # Unix epoch seconds at the end of the current minute window

    def headers(self) -> dict[str, str]:
        return {
            RATELIMIT_LIMIT_HEADER: str(self.limit),
            RATELIMIT_REMAINING_HEADER: str(self.remaining),
            RATELIMIT_RESET_HEADER: str(self.reset),
        }


class RateLimiter:
    """A fixed 60-second window plus a UTC-daily cap, both global, both in memory.

    Global rather than per-caller because there is exactly one credential: with
    one key, per-credential and global are the same set. Step 04 introduces real
    identities and will need a key per caller here.

    **In-process, and therefore correct only for a single worker.** Four uvicorn
    workers means four independent counters and four times the limit. The README
    run command pins `--workers 1` for this reason, and the startup log says so
    out loud rather than leaving it to be discovered from a bill.

    No lock: `acquire()` contains no `await`, so under asyncio it runs to
    completion without interleaving. That property is what makes the
    read-modify-write safe, and it is why this method must stay synchronous.

    `clock` is injected so tests can cross a window boundary without sleeping.

    ponytail: two counters and a wall clock. A sliding window or a shared store
    (Redis) is the upgrade path when multi-worker or smoother bursting matters.
    """

    def __init__(
        self,
        per_minute: int,
        daily_cap: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._per_minute = per_minute
        self._daily_cap = daily_cap
        self._clock = clock
        self._minute = -1
        self._minute_count = 0
        self._day = -1
        self._day_count = 0

    def acquire(self) -> LimitDecision:
        """Charge one request, or raise `AppError` (429) and charge nothing.

        Order is the point: the per-minute window is checked first, then the
        daily cap, and a counter moves only when BOTH admit the request. A
        refused request must not consume the budget it was refused for.
        """
        now = self._clock()
        minute = int(now // _SECONDS_PER_MINUTE)
        day = int(now // _SECONDS_PER_DAY)

        if minute != self._minute:
            self._minute, self._minute_count = minute, 0
        if day != self._day:
            self._day, self._day_count = day, 0

        window_reset = (minute + 1) * _SECONDS_PER_MINUTE
        pending = LimitDecision(
            limit=self._per_minute,
            remaining=max(0, self._per_minute - self._minute_count),
            reset=window_reset,
        )

        if self._minute_count >= self._per_minute:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                429,
                _RATE_LIMITED_MESSAGE,
                headers={
                    **pending.headers(),
                    RETRY_AFTER_HEADER: _retry_after(now, window_reset),
                },
            )

        if self._day_count >= self._daily_cap:
            # The rate headers still describe the MINUTE window, which may well
            # have room left. That is the truth: this request was refused by the
            # day's budget, and `Retry-After` (midnight) is what says so.
            raise AppError(
                ErrorCode.RATE_LIMITED,
                429,
                _DAILY_CAP_MESSAGE,
                headers={
                    **pending.headers(),
                    RETRY_AFTER_HEADER: _retry_after(now, (day + 1) * _SECONDS_PER_DAY),
                },
            )

        self._minute_count += 1
        self._day_count += 1
        return LimitDecision(
            limit=self._per_minute,
            remaining=self._per_minute - self._minute_count,
            reset=window_reset,
        )


def _retry_after(now: float, reset_at: int) -> str:
    """Whole seconds until `reset_at`, never below 1 — `Retry-After: 0` invites
    an immediate retry into the same refusal."""
    return str(max(1, reset_at - int(now)))


async def guard_query(request: Request, response: Response) -> None:
    """Authenticate, then charge the limits. In that order, and in one dependency.

    One function rather than three stacked dependencies because the ORDER is a
    requirement, not an implementation detail: an unauthenticated caller must not
    be able to drain a counter, and the 401 must land before the request body is
    even validated (FastAPI resolves route dependencies before
    `request_body_to_args`, so raising here pre-empts the 422).

    Rate-limit headers are written onto the injected `Response`, which FastAPI
    merges into a 2xx. A 429 carries them through `AppError.headers` instead. A
    5xx raised later in the handler carries neither — a recorded deviation from
    the contract's "headers on every response".
    """
    presented = request.headers.get(CLIENT_KEY_HEADER)
    expected: bytes = request.app.state.client_key_digest

    if presented is None or not hmac.compare_digest(key_digest(presented), expected):
        logger.warning(
            "query credential rejected",
            extra={"path": request.url.path, "reason": "missing" if presented is None else "wrong"},
        )
        raise AppError(ErrorCode.UNAUTHENTICATED, 401, _UNAUTHENTICATED_MESSAGE)

    limiter: RateLimiter = request.app.state.limiter
    response.headers.update(limiter.acquire().headers())
