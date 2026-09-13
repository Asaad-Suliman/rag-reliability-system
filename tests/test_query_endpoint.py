"""POST /api/v1/query — verdict, answer, citations, and input rejection.

    uv run --no-sync python -m tests.test_query_endpoint

Uses `fastapi.testclient.TestClient` (starlette 1.6.0 is installed, so the real
routing, validation and exception-handler stack all run — not the handler
function in isolation).

**`retrieve()` is stubbed at its boundary, and that is deliberate.** A real call
would embed the question, which costs money and needs the network, and would
read `tests/fixtures/query_embeddings.json`, which is untracked and rewritten on
any cache miss. Everything downstream of the stub is the committed code path:
`far_field_gate()`, the response model, the router, the validators, the handlers.
Distances are literals quoted from commit `bc1e261`.

**The LLM is `FakeLLMClient`; the token counter is the REAL `TiktokenCounter`.**
Not `HeuristicCharCounter`: chunk 8.6 found that substituting the heuristic in a
test means the test never exercises the counter production runs, and a budget
assertion made under a different tokenizer is an assertion about nothing. The
tiktoken cache is committed, so the real counter is still $0 and offline.

**Every request now sends `X-API-Key`, and that is a NEW REQUIRED INPUT, not a
test repaired into passing.** Chunk 8.10 made the credential part of the route's
contract; a request without one is *supposed* to fail now, and the assertions
below that prove it are the point. Nothing about an existing expected value,
fixture or threshold was changed to accommodate it.

$0 and offline. No Postgres, no vector store, no embedder, no reranker, no
network, and no paid call of any kind.
"""

from __future__ import annotations

import contextlib
import io
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.router import api_router
from app.core.errors import register_exception_handlers
from app.core.logging import JsonFormatter
from app.core.middleware import RequestIDMiddleware
from app.core.security import (
    CLIENT_KEY_HEADER,
    RATELIMIT_LIMIT_HEADER,
    RATELIMIT_REMAINING_HEADER,
    RATELIMIT_RESET_HEADER,
    RETRY_AFTER_HEADER,
    RateLimiter,
    key_digest,
)
from app.schemas.query import MAX_QUESTION_CHARS
from app.services.context_budget import ContextBudget, TiktokenCounter
from app.services.generation import FakeLLMClient, GenerationFailed, GenerationUpstreamError
from app.services.retrieval import FAR_FIELD_ABSTAIN_DISTANCE, Retrieval, RetrievedChunk

# Quoted from commit `bc1e261`. IN_BAND is a01's top-1 distance minus a hair —
# a01 itself is the constant and is refused. FAR_FIELD is the largest class-1
# out-of-domain top-1 distance recorded.
IN_BAND = 1.0489  # the answerable median
FAR_FIELD = 1.7829


FAKE_ANSWER = "a generated answer from the fake client"

# 32+ chars, the minimum `Settings.client_api_key` enforces. A literal, not a
# generated value: a credential test whose key changes per run cannot assert
# that the key never appears in the logs.
TEST_KEY = "test-client-key-000000000000000000"
AUTH = {CLIENT_KEY_HEADER: TEST_KEY}

TEST_PER_MINUTE = 20
TEST_DAILY_CAP = 200


class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _chunk(distance: float, chunk_id: str = "chk_test", index: int = 0) -> RetrievedChunk:
    """One hit. `index` shifts the character span so every hit in a fixture has
    a DISTINCT `(document_id, char_start, char_end)` locator.

    The old fixture gave every hit the same span, which `provenance.render()`
    rejects as a `DuplicateLocator` — so the fixture described a retrieval that
    cannot exist. Chunk 8.7 open item 4; repaired here because it was wrong,
    not because it was in the way.
    """
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc_01M0D39WZDYY7STA3PHWT5R4C7",
        document_name="Mastering RAG 2026_compressed.pdf",
        page=42 + index,
        char_start=100 + index * 100,
        char_end=200 + index * 100,
        text=f"a retrieved passage ({index})",
        rrf_score=0.0164,
        lexical_rank=None,
        lexical_score=None,
        vector_rank=1,
        vector_distance=distance,
        rerank_score=-2.5,
    )


@contextlib.contextmanager
def _client(
    distance: float,
    *,
    n_hits: int = 2,
    llm: FakeLLMClient | None = None,
    limiter: RateLimiter | None = None,
    retrieve_calls: list[int] | None = None,
) -> Iterator[tuple[TestClient, FakeLLMClient, list[RetrievedChunk]]]:
    """A real app with `retrieve()` stubbed to return `n_hits` at `distance`.

    Yields the client, the fake LLM (so a test can assert the call count) and
    the hits the stub returns (so a test can assert citations against the
    fixture that produced them).
    """
    import app.api.v1.query

    # Deliberate monkeypatch at the retrieve() boundary; `Any` because that is
    # exactly what rebinding a module attribute is, and pretending otherwise
    # would need a type: ignore that says less.
    query_module: Any = app.api.v1.query

    hits = [_chunk(distance, f"chk_{i}", index=i) for i in range(n_hits)]
    fake = llm if llm is not None else FakeLLMClient(text=FAKE_ANSWER)

    async def _stub_retrieve(*args: Any, **kwargs: Any) -> Retrieval:
        # Counting retrieve() is not the same assertion as counting the LLM:
        # retrieval spends a PAID embedding call before the far-field gate can
        # decide whether the model is called at all. A guard that stopped the
        # model but not the embedder would still be a spend leak.
        if retrieve_calls is not None:
            retrieve_calls[0] += 1
        return Retrieval(hits=hits)

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[None]:
        yield None

    original = query_module.retrieve
    query_module.retrieve = _stub_retrieve
    try:
        # Named `test_app`, not `app`: `import app.api.v1.query` above binds the
        # package name `app` in this scope.
        test_app = FastAPI()
        test_app.add_middleware(RequestIDMiddleware)
        register_exception_handlers(test_app)
        test_app.include_router(api_router)
        test_app.state.settings = object()
        test_app.state.vector_store = object()
        test_app.state.reranker = object()
        test_app.state.embedder = object()
        test_app.state.session_factory = _session
        test_app.state.llm = fake
        # The REAL counter, deliberately — see the module docstring.
        test_app.state.counter = TiktokenCounter()
        test_app.state.budget = ContextBudget()
        # The same two objects `app/main.py`'s lifespan builds — the digest, not
        # the key, and one limiter for the app.
        test_app.state.client_key_digest = key_digest(TEST_KEY)
        test_app.state.limiter = limiter or RateLimiter(TEST_PER_MINUTE, TEST_DAILY_CAP)
        with TestClient(test_app) as client:
            yield client, fake, hits
    finally:
        query_module.retrieve = original


def main() -> None:
    print("in-band question -> ANSWER_UNVERIFIED, answer generated, citations present:")
    with _client(IN_BAND) as (client, fake, hits):
        r = client.post("/api/v1/query", json={"question": "What is a reranker?"}, headers=AUTH)
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["verdict"] == "ANSWER_UNVERIFIED", body
        assert body["top_1_distance"] == IN_BAND, body
        assert len(body["citations"]) == 2, body
        assert body["citations"][0]["vector_distance"] == IN_BAND, body
        assert "NOT ESTABLISHED" in body["verdict_meaning"], body
        assert list(body)[0] == "verdict", f"verdict must be first: {list(body)}"

        # The answer is the fake's text, byte for byte — not a truthy check.
        assert body["answer"] == FAKE_ANSWER, body

        # Called exactly once. A test that only asserts the answer cannot tell
        # one call from three.
        assert fake.calls == 1, fake.calls

        # The question and the retrieved text both reached the model, and the
        # retrieved text is inside the fenced block.
        assert fake.last_user is not None
        assert "What is a reranker?" in fake.last_user, fake.last_user
        assert "<retrieved_context>" in fake.last_user, fake.last_user
        assert hits[0].text in fake.last_user, fake.last_user

        # The system prompt is the committed file, not an inline string.
        assert fake.last_system is not None and "retrieved_context" in fake.last_system

        # Citations are the chunks the model saw. At top_k=5 today that is
        # every hit; asserted as an equality of chunk_ids, in order.
        assert [c["chunk_id"] for c in body["citations"]] == [h.chunk_id for h in hits], body

        # All nine CitationOut fields, against the fixture that produced them
        # (chunk 8.7 open item 3).
        for citation, hit in zip(body["citations"], hits, strict=True):
            assert citation["chunk_id"] == hit.chunk_id, citation
            assert citation["document_id"] == hit.document_id, citation
            assert citation["document_name"] == hit.document_name, citation
            assert citation["page"] == hit.page, citation
            assert citation["char_start"] == hit.char_start, citation
            assert citation["char_end"] == hit.char_end, citation
            assert citation["text"] == hit.text, citation
            assert citation["vector_distance"] == hit.vector_distance, citation
            assert citation["rerank_score"] == hit.rerank_score, citation
        assert set(body["citations"][0]) == {
            "chunk_id",
            "document_id",
            "document_name",
            "page",
            "char_start",
            "char_end",
            "text",
            "vector_distance",
            "rerank_score",
        }, body["citations"][0]

        print(
            f"  200 {body['verdict']} top_1={body['top_1_distance']} "
            f"citations={len(body['citations'])} llm_calls={fake.calls} "
            f"answer={body['answer']!r}"
        )
        print("  all 9 CitationOut fields match the fixture, for both citations")

    print("\nfar-field question -> ABSTAIN, answer null, LLM never called:")
    with _client(FAR_FIELD) as (client, fake, hits):
        r = client.post(
            "/api/v1/query", json={"question": "How deep can a sperm whale dive?"}, headers=AUTH
        )
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["verdict"] == "ABSTAIN_OUT_OF_DOMAIN", body
        assert body["answer"] is None, body
        assert len(body["citations"]) == 2, "a refusal must show what triggered it"
        assert [c["chunk_id"] for c in body["citations"]] == [h.chunk_id for h in hits], body
        # The money assertion: not "it did not raise", but "it was never called".
        assert fake.calls == 0, fake.calls
        print(
            f"  200 {body['verdict']} answer={body['answer']} "
            f"citations={len(body['citations'])} llm_calls={fake.calls}"
        )

    print("\nboundary is inclusive — the constant itself refuses:")
    with _client(FAR_FIELD_ABSTAIN_DISTANCE) as (client, fake, _hits):
        body = client.post("/api/v1/query", json={"question": "boundary"}, headers=AUTH).json()
        assert body["verdict"] == "ABSTAIN_OUT_OF_DOMAIN", body
        assert body["answer"] is None and fake.calls == 0, body
        print(f"  {FAR_FIELD_ABSTAIN_DISTANCE} -> {body['verdict']}, answer null, 0 llm calls")

    print("\ngeneration failures are 503, never a 200 with a partial answer:")
    for label, exc, expected_fragment in (
        (
            "GenerationUpstreamError (timeout / 429 / 5xx)",
            GenerationUpstreamError("boom"),
            "unavailable",
        ),
        (
            "GenerationFailed (truncated / refused / empty)",
            GenerationFailed("no usable text"),
            "No answer could be generated",
        ),
    ):
        with _client(IN_BAND, llm=FakeLLMClient(error=exc)) as (client, fake, _hits):
            r = client.post("/api/v1/query", json={"question": "What is a reranker?"}, headers=AUTH)
            assert r.status_code == 503, (label, r.status_code, r.text)
            err = r.json()["error"]
            assert err["code"] == "UPSTREAM_UNAVAILABLE", (label, err)
            assert expected_fragment in err["message"], (label, err)
            assert fake.calls == 1, (label, fake.calls)
            print(f"  {label}: 503 {err['code']} {err['message']!r}")

    # The two 503s must not carry the same message, or the distinction 8.8 §J
    # asked for exists only in the logs.
    with _client(IN_BAND, llm=FakeLLMClient(error=GenerationUpstreamError("x"))) as (c, _f, _h):
        upstream_message = c.post("/api/v1/query", json={"question": "q"}, headers=AUTH).json()[
            "error"
        ]["message"]
    with _client(IN_BAND, llm=FakeLLMClient(error=GenerationFailed("x"))) as (c, _f, _h):
        failed_message = c.post("/api/v1/query", json={"question": "q"}, headers=AUTH).json()[
            "error"
        ]["message"]
    assert upstream_message != failed_message, upstream_message
    print("  the two 503 detail messages are distinct")

    print("\nrejected at the model level, contract-shaped 422:")
    with _client(IN_BAND) as (client, fake, _hits):
        for label, question in (
            ("empty", ""),
            ("whitespace-only", "   \t\n "),
            ("over-length", "x" * (MAX_QUESTION_CHARS + 1)),
        ):
            r = client.post("/api/v1/query", json={"question": question}, headers=AUTH)
            assert r.status_code == 422, (label, r.status_code, r.text)
            err = r.json()["error"]
            assert err["code"] == "VALIDATION_ERROR", err
            assert "question" in err["details"]["fields"], err
            print(f"  {label}: 422 {err['code']} {err['details']['fields']['question'][:44]}")

        assert fake.calls == 0, "a rejected request must never reach the model"

        # A question at exactly the limit is accepted — the bound is inclusive.
        r = client.post("/api/v1/query", json={"question": "x" * MAX_QUESTION_CHARS}, headers=AUTH)
        assert r.status_code == 200, (r.status_code, r.text)
        print(f"  exactly {MAX_QUESTION_CHARS} chars: 200 (bound is inclusive)")

    print("\nno credential and a wrong credential are indistinguishable 401s:")
    messages: set[str] = set()
    for label, headers in (
        ("missing", {}),
        ("wrong", {CLIENT_KEY_HEADER: "wrong-key-000000000000000000000000"}),
        ("empty", {CLIENT_KEY_HEADER: ""}),
    ):
        calls = [0]
        with _client(IN_BAND, retrieve_calls=calls) as (client, fake, _hits):
            r = client.post("/api/v1/query", json={"question": "q"}, headers=headers)
            assert r.status_code == 401, (label, r.status_code, r.text)
            err = r.json()["error"]
            assert err["code"] == "UNAUTHENTICATED", (label, err)
            assert r.headers.get("X-Request-ID"), (label, dict(r.headers))
            # The two money assertions: nothing paid ran. `retrieve()` is the
            # embedding call, `fake.calls` is the model.
            assert calls[0] == 0, (label, calls)
            assert fake.calls == 0, (label, fake.calls)
            messages.add(err["message"])
            print(f"  {label}: 401 {err['code']} retrieve={calls[0]} llm={fake.calls}")
    assert len(messages) == 1, f"401 messages must be identical, got {messages}"
    print(f"  all three share one message: {messages.pop()!r}")

    print("\na bad credential AND a malformed body -> 401, not 422 (auth runs first):")
    calls = [0]
    with _client(IN_BAND, retrieve_calls=calls) as (client, fake, _hits):
        for label, payload in (
            ("wrong field", {"nope": 1}),
            ("blank question", {"question": "  "}),
        ):
            r = client.post("/api/v1/query", json=payload)
            assert r.status_code == 401, (label, r.status_code, r.text)
            print(f"  {label}: {r.status_code} {r.json()['error']['code']}")
        assert calls[0] == 0 and fake.calls == 0

    print("\nhealth takes no credential and consumes no budget:")
    limiter = RateLimiter(TEST_PER_MINUTE, TEST_DAILY_CAP)
    with _client(IN_BAND, limiter=limiter) as (client, _fake, _hits):
        # Liveness only. `/health/ready` probes `app.state.engine`, which this
        # harness deliberately does not build — that is a gap in the FIXTURE,
        # not in the guard: both health routes hang off a separate router that
        # carries no dependency, so neither can acquire one by accident.
        for _ in range(3):
            r = client.get("/api/v1/health")
            assert r.status_code == 200, r.status_code
            assert RATELIMIT_LIMIT_HEADER not in r.headers, dict(r.headers)
        print("  GET /api/v1/health x3 -> 200, no credential, no rate headers")
        r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
        assert r.headers[RATELIMIT_REMAINING_HEADER] == str(TEST_PER_MINUTE - 1), dict(r.headers)
    print(f"  the first query after them still sees remaining={TEST_PER_MINUTE - 1}")

    print("\nrate headers on a 200, and the counter decrements:")
    with _client(IN_BAND) as (client, _fake, _hits):
        clock_now = None
        for i in range(3):
            r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
            assert r.status_code == 200, (i, r.status_code, r.text)
            assert r.headers[RATELIMIT_LIMIT_HEADER] == str(TEST_PER_MINUTE), dict(r.headers)
            assert r.headers[RATELIMIT_REMAINING_HEADER] == str(TEST_PER_MINUTE - 1 - i), i
            reset = int(r.headers[RATELIMIT_RESET_HEADER])
            assert reset % 60 == 0, reset  # the end of a fixed 60s window
            clock_now = reset
        print(
            f"  3 x 200 with limit={TEST_PER_MINUTE} "
            f"remaining={TEST_PER_MINUTE - 1}..{TEST_PER_MINUTE - 3} reset={clock_now}"
        )

    print(f"\nrequest {TEST_PER_MINUTE + 1} in one window -> 429, and nothing paid runs:")
    clock = FakeClock()
    calls = [0]
    with _client(
        IN_BAND,
        limiter=RateLimiter(TEST_PER_MINUTE, TEST_DAILY_CAP, clock=clock),
        retrieve_calls=calls,
    ) as (client, fake, _hits):
        for i in range(TEST_PER_MINUTE):
            assert (
                client.post("/api/v1/query", json={"question": "q"}, headers=AUTH).status_code
                == 200
            ), i
        assert calls[0] == TEST_PER_MINUTE and fake.calls == TEST_PER_MINUTE

        r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
        assert r.status_code == 429, (r.status_code, r.text)
        err = r.json()["error"]
        assert err["code"] == "RATE_LIMITED", err
        minute_limit_message = err["message"]
        assert r.headers[RATELIMIT_LIMIT_HEADER] == str(TEST_PER_MINUTE), dict(r.headers)
        assert r.headers[RATELIMIT_REMAINING_HEADER] == "0", dict(r.headers)
        window_reset = int(r.headers[RATELIMIT_RESET_HEADER])
        retry_after = int(r.headers[RETRY_AFTER_HEADER])
        assert 1 <= retry_after <= 60, retry_after
        assert retry_after == window_reset - int(clock.now), (retry_after, window_reset, clock.now)
        # Refused, so it cost nothing — the counts have not moved.
        assert calls[0] == TEST_PER_MINUTE, calls
        assert fake.calls == TEST_PER_MINUTE, fake.calls
        print(
            f"  429 {err['code']} remaining=0 reset={window_reset} "
            f"Retry-After={retry_after} retrieve={calls[0]} llm={fake.calls}"
        )

        # A refusal must not consume the window either: still 429, not 428 left.
        assert client.post("/api/v1/query", json={"question": "q"}, headers=AUTH).status_code == 429

        print("\n  after the window rolls over, requests are allowed again:")
        clock.now = window_reset
        r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.headers[RATELIMIT_REMAINING_HEADER] == str(TEST_PER_MINUTE - 1), dict(r.headers)
        assert int(r.headers[RATELIMIT_RESET_HEADER]) == window_reset + 60
        assert calls[0] == TEST_PER_MINUTE + 1, calls
        print(f"  clock -> {window_reset}: 200, remaining back to {TEST_PER_MINUTE - 1}")

    print("\nthe daily cap refuses with Retry-After pointing at UTC midnight:")
    clock = FakeClock(now=float(datetime(2026, 9, 11, 23, 40, tzinfo=UTC).timestamp()))
    cap = 3
    calls = [0]
    with _client(
        IN_BAND,
        limiter=RateLimiter(TEST_PER_MINUTE, cap, clock=clock),
        retrieve_calls=calls,
    ) as (client, fake, _hits):
        for i in range(cap):
            # Step a full minute each time so the PER-MINUTE window can never be
            # what refuses: the only limit left standing is the daily cap.
            clock.now += 60
            assert (
                client.post("/api/v1/query", json={"question": "q"}, headers=AUTH).status_code
                == 200
            ), i
        clock.now += 60
        r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
        assert r.status_code == 429, (r.status_code, r.text)
        err = r.json()["error"]
        assert err["code"] == "RATE_LIMITED", err
        assert "daily" in err["message"].lower(), err
        assert err["message"] != minute_limit_message, "the two 429s must read differently"
        for header in (RATELIMIT_LIMIT_HEADER, RATELIMIT_REMAINING_HEADER, RATELIMIT_RESET_HEADER):
            assert header in r.headers, (header, dict(r.headers))
        retry_after = int(r.headers[RETRY_AFTER_HEADER])
        midnight = int(datetime(2026, 9, 12, tzinfo=UTC).timestamp())
        assert retry_after == midnight - int(clock.now), (retry_after, midnight, clock.now)
        assert calls[0] == cap and fake.calls == cap, (calls, fake.calls)
        print(
            f"  429 {err['code']} Retry-After={retry_after}s to 2026-09-12 00:00 UTC, "
            f"retrieve={calls[0]} (== the cap, not {cap + 1})"
        )

        print("  after UTC midnight the cap resets:")
        clock.now = float(midnight)
        r = client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
        assert r.status_code == 200, (r.status_code, r.text)
        assert calls[0] == cap + 1, calls
        print(f"  clock -> {midnight}: 200, retrieve={calls[0]}")

    print("\nthe credential never reaches the logs:")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous, previous_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        with _client(IN_BAND) as (client, _fake, _hits):
            client.post("/api/v1/query", json={"question": "q"}, headers=AUTH)
            client.post("/api/v1/query", json={"question": "q"}, headers={CLIENT_KEY_HEADER: "x"})
        # Belt and braces: the guard does not log the value, AND the formatter
        # redacts it under either name if a future line ever does.
        logging.getLogger(__name__).warning(
            "a line that wrongly carries the key",
            extra={"client_api_key": TEST_KEY, "x-api-key": TEST_KEY},
        )
        logging.getLogger(__name__).warning("client_api_key=%s", TEST_KEY)
    finally:
        root.handlers, root.level = previous, previous_level
    captured = stream.getvalue()
    assert captured.strip(), "nothing was captured — the assertion below would be vacuous"
    assert TEST_KEY not in captured, captured
    assert "***" in captured, captured
    print(f"  {len(captured.splitlines())} log lines captured, 0 contain the key")

    print(
        "\nok: answer on ANSWER_UNVERIFIED and null on ABSTAIN, fake called 1/0 times, "
        "citations == included with all 9 fields checked, both generation failures 503, "
        "4 input rules hold; 401 is identical for missing/wrong/empty and pre-empts the "
        "422, health is unguarded, the minute window and the daily cap both refuse with "
        "Retry-After and rate headers without spending, both reset, and the key never "
        "appears in a log line"
    )


if __name__ == "__main__":
    main()
