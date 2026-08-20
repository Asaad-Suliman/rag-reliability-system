"""Embedding generation: batching, bounded retries, timeout.

`Embedder` is the seam `ingestion.py` codes against. `VoyageEmbedder` is the
real, paid implementation — nothing in this module calls it until Step 02's
approval gate clears. `FakeEmbedder` is deterministic and free, so the whole
ingestion pipeline can be exercised end to end without a network call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

VOYAGE_EMBEDDINGS_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MAX_TEXTS_PER_REQUEST = 1000
# voyage-4-lite's own ceiling is 1,000,000 tokens/request; batching well under
# that means one oversized document never needs a request-splitting retry.
VOYAGE_MAX_TOKENS_PER_REQUEST = 100_000

EmbeddingInputType = Literal["query", "document"]


class EmbeddingError(Exception):
    """Raised after retries are exhausted, or on a non-retryable response."""


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str], input_type: EmbeddingInputType) -> list[list[float]]:
        """Same length and order as `texts`. Empty input returns `[]`."""
        ...


@dataclass(frozen=True)
class _Batch:
    texts: list[str]
    start_index: int


def batch_by_token_budget(
    texts: list[str],
    token_estimates: list[int],
    max_texts: int,
    max_tokens: int,
) -> list[_Batch]:
    """Group texts into request-sized batches respecting a text-count cap and
    a token budget. Pure and local — makes no network call.
    """
    batches: list[_Batch] = []
    current: list[str] = []
    current_tokens = 0
    start = 0
    for i, (text, tokens) in enumerate(zip(texts, token_estimates, strict=True)):
        if current and (len(current) >= max_texts or current_tokens + tokens > max_tokens):
            batches.append(_Batch(texts=current, start_index=start))
            current = []
            current_tokens = 0
            start = i
        current.append(text)
        current_tokens += tokens
    if current:
        batches.append(_Batch(texts=current, start_index=start))
    return batches


class VoyageEmbedder:
    """Real embedding backend. Costs money — see Step 02's approval gate."""

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-4-lite",
        dimensions: int = 1024,
        timeout: float = 30.0,
        max_retries: int = 4,
        max_texts_per_request: int = VOYAGE_MAX_TEXTS_PER_REQUEST,
        max_tokens_per_request: int = VOYAGE_MAX_TOKENS_PER_REQUEST,
        min_request_interval_seconds: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_texts_per_request = max_texts_per_request
        self._max_tokens_per_request = max_tokens_per_request
        # Only needed on an account without a payment method on file, where
        # Voyage enforces a hard requests-per-minute cap that no amount of
        # retrying gets past — spacing requests out is the only fix. 0.0 (the
        # default) makes this a no-op for a normal account.
        self._min_request_interval_seconds = min_request_interval_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str], input_type: EmbeddingInputType) -> list[list[float]]:
        if not texts:
            return []
        token_estimates = [max(1, len(t) // 4) for t in texts]
        batches = batch_by_token_budget(
            texts, token_estimates, self._max_texts_per_request, self._max_tokens_per_request
        )
        vectors: list[list[float]] = [[] for _ in texts]
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for i, batch in enumerate(batches):
                if i > 0 and self._min_request_interval_seconds > 0:
                    await asyncio.sleep(self._min_request_interval_seconds)
                batch_vectors = await self._embed_batch(client, batch.texts, input_type)
                for offset, vector in enumerate(batch_vectors):
                    vectors[batch.start_index + offset] = vector
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, texts: list[str], input_type: EmbeddingInputType
    ) -> list[list[float]]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await client.post(
                    VOYAGE_EMBEDDINGS_URL,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "input": texts,
                        "model": self._model,
                        "input_type": input_type,
                        "output_dimension": self._dimensions,
                        "truncation": True,
                    },
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                logger.info(
                    "embedding request failed, retrying",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self._max_retries,
                        "reason": str(exc),
                    },
                )
                await self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 200:
                payload = response.json()
                return [item["embedding"] for item in payload["data"]]

            if response.status_code == 429 or response.status_code >= 500:
                # Voyage rejected the request outright — nothing was embedded,
                # nothing was billed. Retry after backing off.
                last_exc = EmbeddingError(
                    f"Voyage returned {response.status_code}: {response.text[:200]}"
                )
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else None
                # Without this, a rate-limited call sleeps in total silence —
                # from a synchronous CLI caller that looks identical to a hang.
                logger.info(
                    "embedding request throttled, retrying",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self._max_retries,
                        "status_code": response.status_code,
                    },
                )
                await self._backoff(attempt, retry_after=delay)
                continue

            # Non-retryable 4xx — burning quota retrying a request that will
            # never succeed is worse than failing fast.
            raise EmbeddingError(
                f"Voyage rejected the request ({response.status_code}): {response.text[:200]}"
            )

        raise EmbeddingError(
            f"Voyage embedding failed after {self._max_retries} attempts"
        ) from last_exc

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = (
            retry_after if retry_after is not None else min(2**attempt, 20) + random.uniform(0, 1)
        )
        await asyncio.sleep(delay)


class FakeEmbedder:
    """Deterministic, $0, no network. Same text -> same vector, every time.

    Exercises every code path downstream of `Embedder` — batching call shape,
    vector persistence, VectorStore upsert/query — without touching Voyage.
    """

    def __init__(self, dimensions: int = 1024, model: str = "fake-embedder") -> None:
        self._dimensions = dimensions
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str], input_type: EmbeddingInputType) -> list[list[float]]:
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        repeats = (self._dimensions // len(digest)) + 1
        raw = (digest * repeats)[: self._dimensions]
        return [(b / 127.5) - 1.0 for b in raw]


def _demo() -> None:
    # Batching respects both caps and never inspects network state.
    texts = [f"chunk {i}" for i in range(5)]
    tokens = [40, 40, 40, 40, 40]
    batches = batch_by_token_budget(texts, tokens, max_texts=2, max_tokens=1000)
    assert [len(b.texts) for b in batches] == [2, 2, 1], batches
    assert [b.start_index for b in batches] == [0, 2, 4]

    batches_by_tokens = batch_by_token_budget(texts, tokens, max_texts=100, max_tokens=90)
    assert [len(b.texts) for b in batches_by_tokens] == [2, 2, 1], batches_by_tokens

    async def run() -> None:
        fake = FakeEmbedder(dimensions=16)
        vectors = await fake.embed(["hello world", "hello world", "different text"], "document")
        assert len(vectors) == 3
        assert all(len(v) == 16 for v in vectors)
        assert vectors[0] == vectors[1], "same text must give the same vector"
        assert vectors[0] != vectors[2], "different text must give a different vector"
        assert await fake.embed([], "document") == []

    asyncio.run(run())
    print("ok: batching respects both caps, FakeEmbedder is deterministic and dimension-correct")


if __name__ == "__main__":
    _demo()
