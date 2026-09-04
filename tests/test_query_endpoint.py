"""POST /api/v1/query — verdict, citations, and input rejection.

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

$0 and offline. No Postgres, no vector store, no embedder, no reranker.
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
from app.services.retrieval import FAR_FIELD_ABSTAIN_DISTANCE, Retrieval, RetrievedChunk

# Quoted from commit `bc1e261`. IN_BAND is a01's top-1 distance minus a hair —
# a01 itself is the constant and is refused. FAR_FIELD is the largest class-1
# out-of-domain top-1 distance recorded.
IN_BAND = 1.0489  # the answerable median
FAR_FIELD = 1.7829


def _chunk(distance: float, chunk_id: str = "chk_test") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc_01M0D39WZDYY7STA3PHWT5R4C7",
        document_name="Mastering RAG 2026_compressed.pdf",
        page=42,
        char_start=100,
        char_end=200,
        text="a retrieved passage",
        rrf_score=0.0164,
        lexical_rank=None,
        lexical_score=None,
        vector_rank=1,
        vector_distance=distance,
        rerank_score=-2.5,
    )


@contextlib.contextmanager
def _client(distance: float, *, n_hits: int = 2) -> Iterator[TestClient]:
    """A real app with `retrieve()` stubbed to return `n_hits` at `distance`."""
    import app.api.v1.query

    # Deliberate monkeypatch at the retrieve() boundary; `Any` because that is
    # exactly what rebinding a module attribute is, and pretending otherwise
    # would need a type: ignore that says less.
    query_module: Any = app.api.v1.query

    hits = [_chunk(distance, f"chk_{i}") for i in range(n_hits)]

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
        with TestClient(test_app) as client:
            yield client
    finally:
        query_module.retrieve = original


def main() -> None:
    print("in-band question -> ANSWER_UNVERIFIED, citations present:")
    with _client(IN_BAND) as client:
        r = client.post("/api/v1/query", json={"question": "What is a reranker?"})
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["verdict"] == "ANSWER_UNVERIFIED", body
        assert body["top_1_distance"] == IN_BAND, body
        assert len(body["citations"]) == 2, body
        assert body["citations"][0]["vector_distance"] == IN_BAND, body
        assert "NOT ESTABLISHED" in body["verdict_meaning"], body
        assert "answer" not in body, "no answer field may exist — generation is not implemented"
        assert list(body)[0] == "verdict", f"verdict must be first: {list(body)}"
        print(
            f"  200 {body['verdict']} top_1={body['top_1_distance']} "
            f"citations={len(body['citations'])}"
        )

    print("\nfar-field question -> ABSTAIN_OUT_OF_DOMAIN, citations still present:")
    with _client(FAR_FIELD) as client:
        r = client.post("/api/v1/query", json={"question": "How deep can a sperm whale dive?"})
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["verdict"] == "ABSTAIN_OUT_OF_DOMAIN", body
        assert len(body["citations"]) == 2, "a refusal must show what triggered it"
        print(
            f"  200 {body['verdict']} top_1={body['top_1_distance']} "
            f"citations={len(body['citations'])}"
        )

    print("\nboundary is inclusive — the constant itself refuses:")
    with _client(FAR_FIELD_ABSTAIN_DISTANCE) as client:
        body = client.post("/api/v1/query", json={"question": "boundary"}).json()
        assert body["verdict"] == "ABSTAIN_OUT_OF_DOMAIN", body
        print(f"  {FAR_FIELD_ABSTAIN_DISTANCE} -> {body['verdict']}")

    print("\nrejected at the model level, contract-shaped 422:")
    with _client(IN_BAND) as client:
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

        # A question at exactly the limit is accepted — the bound is inclusive.
        r = client.post("/api/v1/query", json={"question": "x" * MAX_QUESTION_CHARS})
        assert r.status_code == 200, (r.status_code, r.text)
        print(f"  exactly {MAX_QUESTION_CHARS} chars: 200 (bound is inclusive)")

    print("\nok: both verdicts carry citations, no answer field, 4 input rules hold")


if __name__ == "__main__":
    main()
