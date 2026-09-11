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

$0 and offline. No Postgres, no vector store, no embedder, no reranker, no
network, and no paid call of any kind.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.router import api_router
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestIDMiddleware
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
    distance: float, *, n_hits: int = 2, llm: FakeLLMClient | None = None
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
        with TestClient(test_app) as client:
            yield client, fake, hits
    finally:
        query_module.retrieve = original


def main() -> None:
    print("in-band question -> ANSWER_UNVERIFIED, answer generated, citations present:")
    with _client(IN_BAND) as (client, fake, hits):
        r = client.post("/api/v1/query", json={"question": "What is a reranker?"})
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
        r = client.post("/api/v1/query", json={"question": "How deep can a sperm whale dive?"})
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
        body = client.post("/api/v1/query", json={"question": "boundary"}).json()
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
            r = client.post("/api/v1/query", json={"question": "What is a reranker?"})
            assert r.status_code == 503, (label, r.status_code, r.text)
            err = r.json()["error"]
            assert err["code"] == "UPSTREAM_UNAVAILABLE", (label, err)
            assert expected_fragment in err["message"], (label, err)
            assert fake.calls == 1, (label, fake.calls)
            print(f"  {label}: 503 {err['code']} {err['message']!r}")

    # The two 503s must not carry the same message, or the distinction 8.8 §J
    # asked for exists only in the logs.
    with _client(IN_BAND, llm=FakeLLMClient(error=GenerationUpstreamError("x"))) as (c, _f, _h):
        upstream_message = c.post("/api/v1/query", json={"question": "q"}).json()["error"][
            "message"
        ]
    with _client(IN_BAND, llm=FakeLLMClient(error=GenerationFailed("x"))) as (c, _f, _h):
        failed_message = c.post("/api/v1/query", json={"question": "q"}).json()["error"]["message"]
    assert upstream_message != failed_message, upstream_message
    print("  the two 503 detail messages are distinct")

    print("\nrejected at the model level, contract-shaped 422:")
    with _client(IN_BAND) as (client, fake, _hits):
        for label, question in (
            ("empty", ""),
            ("whitespace-only", "   \t\n "),
            ("over-length", "x" * (MAX_QUESTION_CHARS + 1)),
        ):
            r = client.post("/api/v1/query", json={"question": question})
            assert r.status_code == 422, (label, r.status_code, r.text)
            err = r.json()["error"]
            assert err["code"] == "VALIDATION_ERROR", err
            assert "question" in err["details"]["fields"], err
            print(f"  {label}: 422 {err['code']} {err['details']['fields']['question'][:44]}")

        assert fake.calls == 0, "a rejected request must never reach the model"

        # A question at exactly the limit is accepted — the bound is inclusive.
        r = client.post("/api/v1/query", json={"question": "x" * MAX_QUESTION_CHARS})
        assert r.status_code == 200, (r.status_code, r.text)
        print(f"  exactly {MAX_QUESTION_CHARS} chars: 200 (bound is inclusive)")

    print(
        "\nok: answer on ANSWER_UNVERIFIED and null on ABSTAIN, fake called 1/0 times, "
        "citations == included with all 9 fields checked, both generation failures 503, "
        "4 input rules hold"
    )


if __name__ == "__main__":
    main()
