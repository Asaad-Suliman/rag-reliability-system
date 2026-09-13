"""Query endpoint — retrieval, the far-field gate, then generation.

**Authentication and request limits now guard this route; CORS still does not
exist.** `guard_query` (`app/core/security.py`) is attached to the router, not
to the handler, so every route added under this prefix inherits it and cannot
be added unguarded by omission. It checks the shared `X-API-Key` credential,
then the per-minute limit, then the daily cap, and it runs before the request
body is validated — so a caller with no credential gets a 401 and never learns
whether their body was well-formed.

That credential is an interim MACHINE key (chunk 8.10), not a user identity.
Step 04 still owns JWT issuing/verification, password hashing and the
current-user dependency, and decides then whether this path survives beside
them. CORS remains absent: this service is same-origin-only until Step 05.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.security import guard_query
from app.schemas.query import CitationOut, QueryRequest, QueryResponse
from app.services.context_budget import ContextBudget, TokenCounter, plan_context
from app.services.generation import LLMClient, build_user_message, load_prompt
from app.services.provenance import render
from app.services.reranking import RERANK_N, Reranker
from app.services.retrieval import (
    DEFAULT_CANDIDATE_K,
    DEFAULT_TOP_K,
    FAR_FIELD_ABSTAIN_DISTANCE,
    FarFieldVerdict,
    far_field_gate,
    retrieve,
)
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

# The guard sits on the ROUTER, deliberately. On the handler it would be one
# decorator away from being forgotten by the next route added here; on the
# router, forgetting it is impossible without deleting this line. Health is a
# separate router and is untouched by construction — it takes no credential and
# consumes no budget, which is what lets a load balancer keep probing a service
# whose daily cap is exhausted.
router = APIRouter(prefix="/query", tags=["query"], dependencies=[Depends(guard_query)])

# Said in prose in the response so a human reading raw JSON gets the verdict's
# meaning without consulting this source. ANSWER is present for completeness of
# the type and is not reachable from `far_field_gate()` — see its docstring.
_VERDICT_MEANING: dict[FarFieldVerdict, str] = {
    "ANSWER": (
        "The retrieved context supports an answer. Not reachable on this path: no "
        "groundedness signal is committed that could license it."
    ),
    "ABSTAIN_OUT_OF_DOMAIN": (
        "Refused. The question's nearest passage is at or beyond "
        f"{FAR_FIELD_ABSTAIN_DISTANCE}, the far-field cut, so the corpus is judged not to "
        "cover this subject. The citations below are what triggered the refusal."
    ),
    "ANSWER_UNVERIFIED": (
        "The corpus is near enough to be relevant, so an answer was generated from the "
        "passages below. Whether those passages actually support that answer is NOT "
        "ESTABLISHED — no groundedness check exists in this system."
    ),
}

# Read once at import, not per request: the prompt is a committed file, so a
# per-request read would be a filesystem hit that can only ever return the same
# bytes.
_SYSTEM_PROMPT = load_prompt("answer_v1")


@router.post("", response_model=QueryResponse, summary="Retrieve and judge")
async def query(request: Request, body: QueryRequest) -> QueryResponse:
    """Retrieve, judge far-field, return the evidence either way.

    Always 200 on a successful judgement, **including a refusal**:
    ABSTAIN_OUT_OF_DOMAIN is this endpoint working, not failing, so it is not a
    4xx. Only an unjudgeable retrieval is an error, and that leaves as a 503 via
    `FarFieldInputError` (see `app/core/errors.py`).

    On ABSTAIN_OUT_OF_DOMAIN the model is never called: `answer` is null and
    the citations are the retrieval that triggered the refusal. On
    ANSWER_UNVERIFIED the retrieval is budgeted, rendered and generated from,
    and the citations are the chunks the model actually saw — `included`, not
    every hit. Nothing here checks that the answer follows from them.

    A generation failure is a 503, never a 200 carrying a partial answer (see
    `app/core/errors.py`).
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

    decision = far_field_gate(result)
    logger.info(
        "query judged",
        extra={"verdict": decision.verdict, "top_1_distance": decision.top1_distance},
    )

    answer: str | None = None
    cited = result.hits
    if decision.verdict != "ABSTAIN_OUT_OF_DOMAIN":
        counter: TokenCounter = request.app.state.counter
        budget: ContextBudget = request.app.state.budget
        llm: LLMClient = request.app.state.llm

        budgeted = plan_context(result.hits, budget, counter)
        rendered = render(budgeted.included, counter.count)
        generated = await llm.generate(
            _SYSTEM_PROMPT, build_user_message(body.question.strip(), rendered.text)
        )
        answer = generated.text
        # The chunks the model saw, not every hit retrieved: a citation the
        # model never read is not evidence for what it wrote.
        cited = budgeted.included

    return QueryResponse(
        verdict=decision.verdict,
        verdict_meaning=_VERDICT_MEANING[decision.verdict],
        top_1_distance=decision.top1_distance,
        answer=answer,
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
            for hit in cited
        ],
    )
