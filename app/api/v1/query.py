"""Query endpoint — retrieval plus the far-field Guardrail. No generation.

**Before any deployment this route needs authentication, rate limiting and
CORS. None of the three exists in this codebase** — see `app/core/security.py`,
which records that JWT issuing/verification, password hashing and the
current-user dependency "arrive in Step 04 (API and Auth)". This module does
not add them: doing so here would invent a policy Step 04 owns. It names them
so an unauthenticated, unlimited, same-origin-only route is a known state
rather than an oversight.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.schemas.query import CitationOut, QueryRequest, QueryResponse
from app.services.reranking import RERANK_N, Reranker
from app.services.retrieval import (
    DEFAULT_CANDIDATE_K,
    DEFAULT_TOP_K,
    GUARDRAIL_FAR_FIELD_DISTANCE,
    GuardrailVerdict,
    guardrail,
    retrieve,
)
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/query", tags=["query"])

# Said in prose in the response so a human reading raw JSON gets the verdict's
# meaning without consulting this source. ANSWER is present for completeness of
# the type and is not reachable from `guardrail()` — see its docstring.
_VERDICT_MEANING: dict[GuardrailVerdict, str] = {
    "ANSWER": (
        "The retrieved context supports an answer. Not reachable on this path: no "
        "groundedness signal is committed that could license it."
    ),
    "ABSTAIN_OUT_OF_DOMAIN": (
        "Refused. The question's nearest passage is at or beyond "
        f"{GUARDRAIL_FAR_FIELD_DISTANCE}, the far-field cut, so the corpus is judged not to "
        "cover this subject. The citations below are what triggered the refusal."
    ),
    "ANSWER_UNVERIFIED": (
        "The corpus is near enough to be relevant, and these are the passages retrieved. "
        "Whether they actually support an answer is NOT ESTABLISHED — no groundedness "
        "check exists, and no answer is generated."
    ),
}


@router.post("", response_model=QueryResponse, summary="Retrieve and judge")
async def query(request: Request, body: QueryRequest) -> QueryResponse:
    """Retrieve, judge far-field, return the evidence either way.

    Always 200 on a successful judgement, **including a refusal**:
    ABSTAIN_OUT_OF_DOMAIN is this endpoint working, not failing, so it is not a
    4xx. Only an unjudgeable retrieval is an error, and that leaves as a 503 via
    `GuardrailInputError` (see `app/core/errors.py`).

    No answer is produced. Generation is not implemented anywhere in this
    codebase; this route returns the verdict and its citations and stops.
    """
    store: VectorStore = request.app.state.vector_store
    reranker: Reranker = request.app.state.reranker

    async with request.app.state.session_factory() as session:
        result = await retrieve(
            body.question.strip(),
            session,
            store,
            request.app.state.embedder,
            top_k=DEFAULT_TOP_K,
            candidate_k=DEFAULT_CANDIDATE_K,
            arm="hybrid",
            reranker=reranker,
            rerank_n=RERANK_N,
        )

    decision = guardrail(result)
    logger.info(
        "query judged",
        extra={"verdict": decision.verdict, "top_1_distance": decision.top1_distance},
    )
    return QueryResponse(
        verdict=decision.verdict,
        verdict_meaning=_VERDICT_MEANING[decision.verdict],
        top_1_distance=decision.top1_distance,
        citations=[
            CitationOut(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                document_name=hit.document_name,
                page=hit.page,
                char_start=hit.char_start,
                char_end=hit.char_end,
                text=hit.text,
                vector_distance=hit.vector_distance,
                rerank_score=hit.rerank_score,
            )
            for hit in result.hits
        ],
    )
