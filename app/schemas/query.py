"""Query payloads — the far-field gate's request and response shape, with an answer.

`answer: str | None` exists as of chunk 8.9, because generation now exists
(`app/services/generation.py`). The field was deliberately absent until then:
a null that never fills is a promise the system cannot keep. It is still null
on `ABSTAIN_OUT_OF_DOMAIN`, where the model is never called, and that null is
a statement about the verdict, not an unfilled placeholder.

Nothing here claims the answer is grounded in the citations beside it. No
groundedness check exists; that is the Verifier's job and it is not built.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.services.context_budget import CORPUS_MAX_CHUNK_CHARS
from app.services.retrieval import FarFieldVerdict

# The longest question this system will accept, in characters.
#
# Not a number picked for this module: it is `CORPUS_MAX_CHUNK_CHARS`
# (`app/services/context_budget.py`), the measured longest chunk in the frozen
# 260-chunk corpus, re-asserted against the live maximum by that module's
# `_demo()`. A question longer than the longest passage the corpus contains is
# past anything this system has been measured on, and the bound moves only when
# the corpus does.
#
# It is a bound, not *the* limit. The reranker independently refuses a query
# over its own token ceiling (`reranking.py`, `RerankError` — "refusing to
# truncate the query"), which is measured in tokens against a model config that
# is not committed, so it cannot be expressed here. That guard still fires, and
# can fire below this bound.
MAX_QUESTION_CHARS = CORPUS_MAX_CHUNK_CHARS


class QueryRequest(BaseModel):
    """One question. Rejected at the model level rather than in the handler, so
    a bad request is a contract-shaped 422 from `validation_exception_handler`
    and never reaches retrieval.
    """

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """`min_length` alone accepts "   ", which is not a question."""
        if not value.strip():
            raise ValueError("question cannot be empty or whitespace-only")
        return value


class CitationOut(BaseModel):
    """One retrieved chunk, with the provenance a caller needs to check it.

    Fields follow `RetrievedChunk`'s own docstring: `rrf_score` is deliberately
    **not** exposed — it records it as "a fusion artifact (~0.01-0.03), not a
    calibrated relevance number", and says the contract's score comes from the
    reranker instead. `rerank_score` is that number and is None when no
    reranker ran; `vector_distance` is None when the vector arm did not return
    this chunk.
    """

    chunk_id: str
    document_id: str
    document_name: str
    page: int
    char_start: int
    char_end: int
    text: str
    vector_distance: float | None
    rerank_score: float | None


class QueryResponse(BaseModel):
    """The verdict first, then the evidence for it.

    `verdict` is the first field so a client reading only the top level cannot
    miss it, and `verdict_meaning` carries the same statement in prose for a
    human reading the raw JSON. Citations are returned in **both** verdict
    cases: a refusal a caller cannot inspect is a refusal they have to take on
    trust.

    `answer` is null on a refusal (the model is never called) and is placed
    after the verdict fields for the same reason: the verdict is what qualifies
    the answer, so it is read first.
    """

    verdict: FarFieldVerdict
    verdict_meaning: str
    top_1_distance: float
    answer: str | None
    citations: list[CitationOut]
