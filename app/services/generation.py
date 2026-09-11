"""Generate a plain-text answer from budgeted, rendered context.

Designed on paper first: every choice here is pre-registered in
`docs/DECISIONS.md`, 2026-09-11, "Chunk 8.8 — generation". Deviating from that
entry is a decision to record, not a detail to improvise.

**One attempt, no retries.** The JSON-validation and repair-retry loop that
`Step 03 — Agents.md:128` describes belongs to the Verifier's structured
output, not to this plain-text call. A partial or empty completion is a
failure here, never a success with less text in it.

**Success is narrow on purpose.** `stop_reason == "end_turn"` *and* non-empty
stripped text. Every other stop reason -- `max_tokens`, `refusal`,
`stop_sequence`, `tool_use`, `pause_turn`, and any value Anthropic adds after
this was written -- is a generation failure. A refusal in particular arrives as
HTTP 200 with possibly empty content, so status alone cannot be trusted to
mean "an answer came back".

Transport talks to the Anthropic Messages API over `httpx` directly, mirroring
`app/services/embeddings.py`; no SDK is added for one endpoint. The API key,
the system prompt and the user message are never logged -- latency, token
usage, model and stop reason are.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

# Pinned, not "latest": the response shape this module parses is the one this
# version defines. Verified against Anthropic's documentation 2026-09-11.
ANTHROPIC_VERSION = "2023-06-01"

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents" / "prompts"

# The delimiters that fence retrieved text off from the instructions. Any
# occurrence of either inside the question or the context is neutralised before
# insertion (`build_user_message`) -- otherwise a passage containing the closing
# tag could end the block early and have its remainder read as instructions.
CONTEXT_OPEN = "<retrieved_context>"
CONTEXT_CLOSE = "</retrieved_context>"

# The only stop reason that means "the model finished saying what it had to say".
_SUCCESS_STOP_REASON = "end_turn"


class GenerationUpstreamError(RuntimeError):
    """The provider could not be reached, or could not serve the request now.

    Timeout, transport error, 429, and any 5xx (529 `overloaded_error`
    included). Retryable from the caller's point of view, so it leaves the app
    as a 503 -- the same thing the readiness probe already means by 503.
    """


class GenerationFailed(RuntimeError):
    """The provider answered, and the answer is not usable.

    A stop reason other than `end_turn` (truncation, refusal, an unknown
    future value) or an empty/whitespace-only completion. Distinct from
    `GenerationUpstreamError` because nothing is down: the call succeeded and
    produced no answer this system is willing to return as one.
    """


class ProviderRequestError(RuntimeError):
    """A 4xx other than 429 -- a bad key, a bad model name, a malformed body.

    Not an outage and not retryable: it is a configuration or request defect on
    this side, so it surfaces as a 500 rather than a 503. (8.8 §J does not
    cover non-429 4xx; decided in 8.9 and recorded in that entry.)
    """


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class LLMClient(Protocol):
    async def generate(self, system: str, user: str) -> GenerationResult: ...


def load_prompt(name: str) -> str:
    """Read a versioned prompt file. Prompts live on disk, never inline
    (`Step 03 — Agents.md:116`), so a prompt change is a reviewable diff.
    """
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _neutralise(value: str) -> str:
    """Defang both fence tags wherever they appear in untrusted text.

    Escaping the angle brackets keeps the text readable to the model while
    making it impossible for a passage -- or a question -- to close the block
    early and have what follows read as instructions.
    """
    return value.replace(CONTEXT_OPEN, "&lt;retrieved_context&gt;").replace(
        CONTEXT_CLOSE, "&lt;/retrieved_context&gt;"
    )


def build_user_message(question: str, rendered_text: str) -> str:
    """The user turn: the question, then the retrieved passages in a fenced,
    clearly-untrusted block. Pure -- no I/O, no network, same bytes every run.
    """
    return (
        f"{_neutralise(question)}\n\n{CONTEXT_OPEN}\n{_neutralise(rendered_text)}\n{CONTEXT_CLOSE}"
    )


def _parse_response(payload: dict[str, object], latency_ms: float) -> GenerationResult:
    """Turn a 200 body into a result, or raise `GenerationFailed`.

    The answer is the concatenation of the text of every `type == "text"`
    block; other block types contribute nothing. A refusal can carry an empty
    `content` list, which is why emptiness is checked after concatenation and
    not before.
    """
    stop_reason = str(payload.get("stop_reason"))
    blocks = payload.get("content")
    text = ""
    if isinstance(blocks, list):
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    usage = payload.get("usage")
    usage_dict: dict[str, object] = usage if isinstance(usage, dict) else {}

    def _tokens(key: str) -> int:
        value = usage_dict.get(key)
        # Token counts are reporting, not control flow: a body without them is
        # still a valid answer, so an unexpected type reports 0 rather than
        # turning a successful generation into a crash.
        return value if isinstance(value, int) else 0

    input_tokens = _tokens("input_tokens")
    output_tokens = _tokens("output_tokens")

    if stop_reason != _SUCCESS_STOP_REASON:
        raise GenerationFailed(
            f"the model stopped with stop_reason={stop_reason!r}, not "
            f"{_SUCCESS_STOP_REASON!r}; a truncated, refused or otherwise "
            "incomplete completion is never returned as an answer"
        )
    if not text.strip():
        raise GenerationFailed(
            "the model returned an empty completion (no non-whitespace text "
            f"blocks) with stop_reason={stop_reason!r}"
        )

    return GenerationResult(
        text=text,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )


class AnthropicClient:
    """The Messages API over one long-lived `httpx.AsyncClient`.

    Built once in `app/main.py`'s lifespan and closed on shutdown, like the
    embedder and the reranker. Construction opens no connection and costs
    nothing.
    """

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        max_tokens: int,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, system: str, user: str) -> GenerationResult:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                ANTHROPIC_MESSAGES_URL,
                headers={
                    "x-api-key": self._api_key.get_secret_value(),
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GenerationUpstreamError(
                f"the generation provider could not be reached: {type(exc).__name__}"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code

        # Dispatch on the status code alone. The error-body shape is the
        # provider's to change, and a 503-vs-500 decision must not depend on
        # being able to parse it.
        if status == 429 or status >= 500:
            raise GenerationUpstreamError(
                f"the generation provider returned HTTP {status} and cannot serve this request now"
            )
        if status >= 400:
            raise ProviderRequestError(
                f"the generation provider rejected the request with HTTP {status}; "
                "this is a configuration or request defect, not an outage"
            )

        result = _parse_response(response.json(), latency_ms)
        logger.info(
            "generation completed",
            extra={
                "model": self._model,
                "latency_ms": round(result.latency_ms, 1),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "stop_reason": result.stop_reason,
            },
        )
        return result


class FakeLLMClient:
    """Deterministic stand-in. The default everywhere in the test suite (8.8
    §N): every test in this repo is $0 and offline, and a real call happens
    only as a single, explicitly-consented smoke test.

    Records how many times it was called and with what, so a test can assert
    the abstain path never calls it -- a claim "no exception was raised"
    cannot make.
    """

    def __init__(self, text: str = "a generated answer", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.last_system: str | None = None
        self.last_user: str | None = None

    async def generate(self, system: str, user: str) -> GenerationResult:
        self.calls += 1
        self.last_system = system
        self.last_user = user
        if self.error is not None:
            raise self.error
        return GenerationResult(
            text=self.text,
            stop_reason=_SUCCESS_STOP_REASON,
            input_tokens=100,
            output_tokens=20,
            latency_ms=1.0,
        )
