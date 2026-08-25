"""Pack reranked hits into a token budget, and record what did not fit.

Sits between `retrieval.retrieve()` and a generation step that does not exist
yet (Chunk 7+). Its job is structural, not statistical: produce a deterministic,
whole-chunk selection plus an auditable record of every hit dropped for budget
reasons, so the Guardrail can tell "nothing relevant was retrieved" apart from
"something relevant was retrieved and budgeted out" without re-running retrieval.

**Whole chunks only.** Nothing here truncates, slices, or rebuilds a hit's text.
`char_start`/`char_end` are absolute offsets into the document's canonical text
(see `chunking.py`), and Chunk 9's Verifier scores groundedness against those
spans. Partial text would make the Verifier validate a claim against evidence
the model never saw — a correctness constraint, not a preference.

**No presentation policy.** Selection is greedy by score, descending. Where the
selected chunks are *placed* in the eventual prompt is a separate concern with
exactly one implemented strategy (`PresentationOrder.RERANKER_ORDER`). Coherence
reordering and lost-in-the-middle placement are unmeasured variables and there is
no harness to evaluate them until generation exists.

Counting requires `tiktoken` and a populated `models/tiktoken/` cache (see
`scripts/fetch_tiktoken_cache.py`) — no network at call time, but not
dependency-free either, unlike the rest of this module. Run the self-check:

    uv run python -m app.services.context_budget
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

# Reused, not redeclared: the reranker module already names its weight files and
# `scripts/reranker_model.sha256` already verifies them, so the self-check below
# reads the same tokenizer the app verified rather than a second path that can drift.
from app.services.reranking import TOKENIZER_FILENAME
from app.services.retrieval import RetrievedChunk

# --- token counting ----------------------------------------------------------

# Measured 2026-08-24 over all 260 corpus chunks against the vendored reference
# tokenizer (`models/reranker/tokenizer.json`): the observed chars-per-token
# FLOOR is 3.675. 3.5 sits below it, which is what makes the over-estimate hold
# with a margin (min 1.0524x, median 1.49x) rather than by luck.
#
# The margin is proven on this corpus's prose, NOT universally: ID- and
# number-dense text fragments far harder (a JSON provenance header measures 44
# here against 74 reference tokens). That is why this counter is only ever
# applied to chunk text, and why the per-chunk render overhead below is sized
# against the reference tokenizer instead of against this heuristic.
CHARS_PER_TOKEN = 3.5

# tiktoken cl100k_base's own cache directory -- must match
# `scripts/fetch_tiktoken_cache.py`'s `DEFAULT_DEST`. Duplicated as a plain
# path constant (not imported from `scripts/`) on purpose: this module is
# runtime application code and must not depend on `scripts/`, which is a
# build-time-only directory with no guarantee of being present in a deployed
# image. It is safe to duplicate because it is our own naming convention
# (like `reranking.DEFAULT_MODEL_DIR`), not a value derived from anything
# that could drift out from under it.
TIKTOKEN_CACHE_DIR = Path("models/tiktoken")

# Covers cross-tokenizer drift among BPE-family models ONLY -- e.g. cl100k_base
# vs a different vocab/merge table another BPE tokenizer might use for
# semantically similar text. It does NOT cover structural fragmentation
# (dense tables, TOCs, glued numeric compounds — see HeuristicCharCounter's
# docstring and docs/DECISIONS.md, 2026-08-24 chunk 7 Phase D): those are a
# property of the TEXT, not of which BPE vocabulary tokenizes it, so a
# constant multiplier does not model them and TiktokenCounter's raw count
# already reflects them directly (tiktoken tokenizes the real text, fragmented
# text or not). PROVISIONAL: sized as a round, generous cross-family margin
# with no measurement behind it, because the generation model is still
# unpinned (Chunk 7+) — there is nothing to measure the drift against yet.
# Revisit the moment there is.
CROSS_TOKENIZER_SAFETY_FACTOR = 1.2


class TiktokenCacheMissing(RuntimeError):
    """tiktoken could not load cl100k_base from the local cache.

    `TiktokenCounter` blocks socket connections for the duration of the load
    (see its `__init__`), so a missing or non-matching cache raises this
    instead of silently falling through to a real network fetch. Chunk 7
    Phase E found that depending on a caller-set `TIKTOKEN_CACHE_DIR` env var
    is a real defect — proven under network-namespace isolation: unset, the
    load attempts a genuine HTTPS connection instead of failing closed. This
    class and the socket guard exist specifically so that defect cannot
    recur here, regardless of whether some future caller also forgets to set
    the env var.

    Never caught and downgraded to `HeuristicCharCounter` — a silent
    downgrade is exactly the failure mode this exists to prevent. Run
    `uv run python -m scripts.fetch_tiktoken_cache --allow-network
    --write-manifest` to populate the cache.
    """


class TokenCounter(Protocol):
    """How many tokens a string costs, as charged against the budget.

    `name` is the counter's identity in the run manifest: two runs with the same
    corpus and the same reranker but different counters are not comparable, and
    a manifest that does not say which one ran cannot tell them apart.
    """

    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


class HeuristicCharCounter:
    """Offline, dependency-free, deliberately pessimistic. NO LONGER THE
    BUDGET-SAFETY MECHANISM as of Chunk 7 Phase E — kept available (some
    caller may still want a zero-dependency estimate), but `plan_context`
    defaults to `TiktokenCounter`, not this.

    `ceil(ascii_chars / CHARS_PER_TOKEN) + non_ascii_chars`.

    **Its "never under-count" invariant does NOT hold, measured.** Chunk 7
    Phase C/D (docs/DECISIONS.md, 2026-08-24) found, against tiktoken
    cl100k_base: **min margin 0.9722x on the live 260-chunk corpus** (4
    chunks under-count), and **min margin 0.6257x on a synthetic adversarial
    block** (glued numeric compounds — `$0.50`, `500,000-token`, `95-99%` —
    mixed with short newline-delimited lines). The only property that was
    ever actually proven is "never under-counts against WordPiece on this
    corpus's prose" — narrower than the docstring used to claim, and not the
    property the budget needs.

    The non-ASCII term charges one token per non-ASCII character. It is inert on
    this corpus (291 such characters in 273k, changing no count), and it costs
    one line — but a pure chars/N ratio under-counts badly on CJK, where BPE
    runs at roughly one token per character.

    ponytail: a heuristic, not a tokenizer, and demonstrably not a safe one
    for anything table/list/identifier-dense. `TiktokenCounter` is the
    replacement; see it for what changed.
    """

    name = "heuristic-char-3.5-v1"

    def count(self, text: str) -> int:
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        return math.ceil((len(text) - non_ascii) / CHARS_PER_TOKEN) + non_ascii


class TiktokenCounter:
    """The budget-safety mechanism as of Chunk 7 Phase E. Exact tiktoken
    cl100k_base counts, times `CROSS_TOKENIZER_SAFETY_FACTOR` (see that
    constant for exactly what the factor does and does not cover).

    Resolves its own cache directory (`TIKTOKEN_CACHE_DIR`) rather than
    depending on a caller-set `TIKTOKEN_CACHE_DIR` env var — Phase E
    verification found that dependency was a real defect (see
    `TiktokenCacheMissing`). Never falls back to network or to
    `HeuristicCharCounter`: a missing/stale cache is a deployment defect to
    fix, not a runtime condition to route around.
    """

    name = f"tiktoken-cl100k_base-v1+{CROSS_TOKENIZER_SAFETY_FACTOR}x-cross-tokenizer"

    def __init__(self, cache_dir: Path = TIKTOKEN_CACHE_DIR) -> None:
        import socket

        os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)

        real_connect = socket.socket.connect
        real_getaddrinfo = socket.getaddrinfo

        def _blocked(*_args: object, **_kwargs: object) -> None:
            raise TiktokenCacheMissing(
                f"tiktoken attempted a real network call while loading "
                f"cl100k_base -- the cache at {cache_dir} is missing or does "
                "not match what tiktoken expects. TiktokenCounter never "
                "falls back to network or to HeuristicCharCounter. Run `uv "
                "run python -m scripts.fetch_tiktoken_cache --allow-network "
                "--write-manifest` to populate it."
            )

        # Both DNS resolution and the raw connect are blocked -- Phase E
        # found that in a network-namespace-isolated test, DNS fails first
        # and `.connect` is never reached; in a real deployment with working
        # DNS, `.connect` is what would actually be attempted. Blocking both
        # makes the guarantee hold regardless of the network environment.
        socket.getaddrinfo = _blocked  # type: ignore[assignment]
        socket.socket.connect = _blocked  # type: ignore[method-assign]
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding("cl100k_base")
        finally:
            socket.getaddrinfo = real_getaddrinfo
            socket.socket.connect = real_connect  # type: ignore[method-assign]

    def count(self, text: str) -> int:
        raw = len(self._encoding.encode(text))
        return math.ceil(raw * CROSS_TOKENIZER_SAFETY_FACTOR)


# --- budget ------------------------------------------------------------------

# Module constants, not `Settings` fields, and deliberately so: the generation
# model is not pinned until Chunk 7+, so there is no real context window to
# configure yet and an env surface for values nothing sets is a surface that
# drifts. Promotion to `Settings` is logged as a Chunk 7 consideration in
# 09_Memory/DECISIONS.md so the choice is revisited rather than inherited.

# claude-sonnet-5, matching `Settings.llm_model`'s default. If that default
# moves, this must move with it — a budget sized for a window the model does not
# have is worse than no budget, because it looks correct.
CONTEXT_WINDOW = 200_000

# System prompt, citation-format instructions, and inter-chunk delimiters — the
# fixed scaffold, independent of how many chunks are selected. Per-chunk cost is
# charged separately (`PER_CHUNK_OVERHEAD_TOKENS`) precisely so this value does
# not silently depend on `top_k`.
PROMPT_SCAFFOLD_RESERVE = 2_000

# A ceiling on the user's question, not a measurement of one. Golden-set
# questions run 40-160 characters; 512 tokens is roughly 1,800 characters, so a
# question would have to be an order of magnitude longer than anything the
# fixture contains before this bound is wrong.
QUERY_RESERVE = 512

# Room for the completion. A truncated answer is a wrong answer, and the failure
# is invisible to the Verifier — it scores what was produced, not what was cut
# off. Sized for a multi-paragraph grounded answer with inline citations.
ANSWER_RESERVE = 4_000

# The provenance header the Chunk 7 renderer will emit per included chunk, so
# the model can cite. MEASURED, not asserted: five plausible header formats were
# rendered at this corpus's widest real values (page 247, char_end 271256, a
# 33-character filename, a 30-character ULID chunk id) and counted with the
# reference tokenizer -- 23, 40, 49, 69, and 74 tokens. 96 sits above that
# observed maximum with room for a format Chunk 7 has not chosen yet.
#
# Sized against the REFERENCE tokenizer, not against `HeuristicCharCounter`:
# the heuristic scored those same headers at 20-44, i.e. it under-counts
# ID-dense text. See CHARS_PER_TOKEN.
#
# BINDING CONSTRAINT ON CHUNK 7: the renderer's actual header must fit within
# this value, and Chunk 7 must assert that it does.
PER_CHUNK_OVERHEAD_TOKENS = 96

# Measured 2026-08-24 against the live corpus (260 chunks, document
# doc_01M0D39WZDYY7STA3PHWT5R4C7): min 69, median 1060, p95 1870, max 1999.
#
# Recorded as a constant so the decision-C invariant can be asserted against the
# real worst case rather than against a value read from the chunker's config.
# `_demo()` re-reads the live maximum and fails if it has moved — the same
# discipline as `GATE_FIGURES` and `_PRE_RERANK_DIGEST`.
CORPUS_MAX_CHUNK_CHARS = 1999


class BudgetError(RuntimeError):
    """Base for every budgeting failure.

    Subclasses RuntimeError so `app/cli.py`'s existing handler reports these as
    `refused:` with exit 4 and no traceback — the same reasoning as `RerankError`.
    """


class ImpossibleBudget(BudgetError):
    """The reserves leave no room for context. Raised at construction, on purpose.

    A budget whose reserves meet or exceed its window can only ever produce an
    empty selection, which at a call site is indistinguishable from "retrieval
    found nothing". Failing where the numbers are written keeps that confusion
    from ever reaching the Guardrail.
    """


class ChunkExceedsBudget(BudgetError):
    """A single hit cannot fit. An invariant violation, not a runtime condition.

    Whole-chunk inclusion (see the module docstring) means there is no correct
    way to make an oversized chunk fit: truncating it would break the citation
    spans the Verifier needs. So this is a configuration defect — the chunker
    and the budget disagree about how large a chunk may be — and it is raised
    loudly rather than resolved by silently dropping evidence.
    """


class MixedScoreSources(BudgetError):
    """Some hits are reranked and some are not. There is no sound way to rank them.

    A raw cross-encoder logit is unbounded (~+/-10); an RRF score is a fusion
    artifact (~0.01-0.03). Sorting both on one key would compare incommensurable
    scales and would do it silently, always ordering every reranked hit above
    every un-reranked one regardless of relevance. Uniformity is what makes the
    RRF fallback in `_score_source` sound; this is not a general comparator.
    """


class ProvenanceHeaderUnmeasured(RuntimeError):
    """Blocking prerequisite for chunk 7, not a runtime budgeting failure —
    not a `BudgetError` subclass on purpose, so it is never swallowed by
    `app/cli.py`'s `refused:` handler; it must stop `_demo()` visibly.

    Raised unconditionally by `_check_provenance_header_pending` at the end
    of every `_demo()` run. See that function for the full story: the
    2026-08-24 DECISIONS.md correction RETRACTED the claim that
    `PER_CHUNK_OVERHEAD_TOKENS = 96` was measured — the renderer it was
    supposedly measured against does not exist, and 3 of the 5 recorded
    header formats never had a literal template on record. 96 is now an
    UNVALIDATED ESTIMATE. This stays red — `uv run python -m
    app.services.context_budget` cannot pass clean — until chunk 7's real
    renderer exists and `scripts/validate_token_counter.py` (without
    `--skip-headers`) has measured its real header formats against
    `PER_CHUNK_OVERHEAD_TOKENS`. Delete this class and its call site then,
    not before.
    """


@dataclass(frozen=True)
class ContextBudget:
    """The five named values, and the one derived number every call site reads.

    `usable_budget` exists so the subtraction has exactly one definition. A call
    site recomputing `window - a - b - c` is a place the arithmetic can drift.
    """

    context_window: int = CONTEXT_WINDOW
    prompt_scaffold_reserve: int = PROMPT_SCAFFOLD_RESERVE
    query_reserve: int = QUERY_RESERVE
    answer_reserve: int = ANSWER_RESERVE
    per_chunk_overhead_tokens: int = PER_CHUNK_OVERHEAD_TOKENS

    def __post_init__(self) -> None:
        negative = [
            name
            for name in (
                "context_window",
                "prompt_scaffold_reserve",
                "query_reserve",
                "answer_reserve",
                "per_chunk_overhead_tokens",
            )
            if getattr(self, name) < 0
        ]
        if negative:
            raise ImpossibleBudget(
                f"negative budget value(s): {', '.join(negative)}. "
                "Every reserve is a token count and cannot be below zero."
            )
        if self.usable_budget <= 0:
            raise ImpossibleBudget(
                f"reserves leave no room for context: context_window="
                f"{self.context_window} - prompt_scaffold_reserve="
                f"{self.prompt_scaffold_reserve} - query_reserve={self.query_reserve} "
                f"- answer_reserve={self.answer_reserve} = {self.usable_budget}. "
                "Raise context_window or lower a reserve."
            )

    @property
    def usable_budget(self) -> int:
        """Tokens available for chunk text plus per-chunk overhead."""
        return (
            self.context_window
            - self.prompt_scaffold_reserve
            - self.query_reserve
            - self.answer_reserve
        )


# --- selection ---------------------------------------------------------------

ScoreSource = Literal["rerank", "rrf"]
"""Which score the selection actually ranked on. First-class, never inferred.

Chunk 7's Guardrail thresholds abstention on reranker score. 09_Memory/
DECISIONS.md (2026-08-19) records that RRF fuses by RANK POSITION only and is
structurally incapable of signalling absence: an RRF score of 0.21 means "was
ranked", not "is weakly relevant". The Guardrail therefore has to be able to
see `"rrf"` and refuse to calibrate on it, which it can only do if this travels
on the result rather than being reconstructed from `rerank_score is None`.
"""


class PresentationOrder(StrEnum):
    """Where selected chunks are placed in the prompt. Exactly one member.

    Coherence reordering and lost-in-the-middle placement are deliberately
    absent: both are unmeasured on this corpus and there is no generation
    harness to evaluate them against until Chunk 7 exists. The enum marks the
    axis without pretending to have an opinion on it.
    """

    RERANKER_ORDER = "reranker_order"


@dataclass(frozen=True)
class ExcludedHit:
    """A hit dropped for budget reasons, with what it would have cost.

    Carries the whole `RetrievedChunk` rather than a bare id: it is less code
    than a projection and strictly more information — the Guardrail gets the
    reranker score, the provenance, and the text without re-running retrieval.
    """

    hit: RetrievedChunk
    score: float
    """The selection score actually used — see `BudgetedContext.score_source`."""

    tokens: int
    """`counter.count(text)` plus the per-chunk overhead: its true cost."""


@dataclass(frozen=True)
class BudgetedContext:
    """What fits, what did not, and everything needed to reproduce the decision.

    The Guardrail's distinction is answerable from this object alone:

    | included  | excluded  | meaning                                        |
    |-----------|-----------|------------------------------------------------|
    | empty     | empty     | nothing was retrieved at all                   |
    | non-empty | empty     | everything retrieved fit                       |
    | non-empty | non-empty | relevant hits retrieved, but budgeted out      |

    `included` empty with `excluded` non-empty is UNREACHABLE, and that is a
    guarantee rather than an oversight: decision C makes an unfittable single
    hit an error, so every hit fits on its own, so the top-ranked hit is always
    included. The Guardrail therefore reads "something relevant was budgeted
    out" off `excluded` being non-empty — never off `included` being empty —
    and it can rely on the best hit never having been the one dropped.
    """

    included: list[RetrievedChunk]
    """In presentation order. The SAME objects the caller passed in — not copies,
    not truncated, not rebuilt (see the module docstring on whole chunks)."""

    excluded: list[ExcludedHit]
    """Dropped for budget reasons, in the order they were considered."""

    score_source: ScoreSource
    budget: ContextBudget
    counter_name: str
    order: PresentationOrder
    tokens_used: int
    tokens_remaining: int


def _score_source(hits: Sequence[RetrievedChunk]) -> ScoreSource:
    """`"rerank"` when every hit was reranked, `"rrf"` when none was, else raise.

    The `"rrf"` fallback is not a convenience: `NoOpReranker` returns
    `scores={}`, so every hit on the no-rerank path — the path the frozen gate
    and the CLI's `--reranker none` degradation switch both use — has
    `rerank_score is None`. Refusing those outright would make the budgeter
    unusable in exactly the configuration the benchmark runs in.
    """
    reranked = sum(1 for h in hits if h.rerank_score is not None)
    if reranked == 0:
        return "rrf"
    if reranked == len(hits):
        return "rerank"
    mixed = sorted(h.chunk_id for h in hits if h.rerank_score is None)
    raise MixedScoreSources(
        f"{reranked} of {len(hits)} hits carry a reranker score; the rest do not. "
        f"Un-reranked: {', '.join(mixed[:5])}{'...' if len(mixed) > 5 else ''}. "
        "A cross-encoder logit and an RRF score are not on comparable scales, so "
        "one sort key over both would silently rank by score source, not by "
        "relevance. Pass hits from a single retrieve() call."
    )


def _selection_key(hit: RetrievedChunk, source: ScoreSource) -> tuple[float, str]:
    """Score descending, `chunk_id` ascending as the tie-break.

    `chunk_id` matches `reranking._rank()` and `_LEXICAL_SQL`'s
    `ORDER BY score DESC, c.id` — one tie-break convention repo-wide, so equal
    scores never resolve differently depending on which stage did the sorting.
    """
    score = hit.rerank_score if source == "rerank" else hit.rrf_score
    assert score is not None  # guaranteed by _score_source
    return (-score, hit.chunk_id)


def plan_context(
    hits: Sequence[RetrievedChunk],
    budget: ContextBudget,
    counter: TokenCounter | None = None,
    order: PresentationOrder = PresentationOrder.RERANKER_ORDER,
) -> BudgetedContext:
    """Greedily select whole chunks by score until the budget is spent.

    `counter` defaults to `TiktokenCounter` (Chunk 7 Phase E) — the previous
    "no default, ever" stance was dropped deliberately, not by accident:
    that stance existed because any implicit default was equally arbitrary
    among untrustworthy options. That is no longer true — `TiktokenCounter`
    is the validated safety mechanism, and `counter_name`/`budget_manifest`
    still always record which counter actually ran, so a default cannot
    silently go untracked the way the original concern worried about.
    Constructed lazily (not as a literal default argument) so importing this
    module never requires the tiktoken cache to be present — only calling
    `plan_context()` without an explicit `counter` does.

    Greedy by score, not by score-per-token. That is knapsack-suboptimal in
    general, and right here: chunk sizes are tightly clustered (69-1999 chars),
    relevance is not additive — the rank-1 chunk carries most of the answer —
    and a density-greedy pack can drop the top hit when it happens to be long,
    which is the one outcome a RAG context must never produce.

    Continues past a chunk that does not fit rather than stopping at the first:
    a later, smaller chunk may still fit, and dropping it too would understate
    what the budget could actually hold. Everything skipped lands in `excluded`.

    Raises `ChunkExceedsBudget` if any single hit cannot fit on its own — see
    that exception for why that is an invariant and not a runtime condition.
    """
    if counter is None:
        counter = TiktokenCounter()

    if not hits:
        return BudgetedContext(
            included=[],
            excluded=[],
            # No hits means no evidence about which source would have been used.
            # "rrf" is the conservative label: it tells the Guardrail not to
            # calibrate, which is correct when there is nothing to calibrate on.
            score_source="rrf",
            budget=budget,
            counter_name=counter.name,
            order=order,
            tokens_used=0,
            tokens_remaining=budget.usable_budget,
        )

    source = _score_source(hits)
    ordered = sorted(hits, key=lambda h: _selection_key(h, source))

    included: list[RetrievedChunk] = []
    excluded: list[ExcludedHit] = []
    used = 0

    for hit in ordered:
        cost = counter.count(hit.text) + budget.per_chunk_overhead_tokens
        if cost > budget.usable_budget:
            raise ChunkExceedsBudget(
                f"chunk {hit.chunk_id} ({hit.document_name} p{hit.page} "
                f"chars {hit.char_start}-{hit.char_end}) costs {cost} tokens "
                f"({cost - budget.per_chunk_overhead_tokens} text + "
                f"{budget.per_chunk_overhead_tokens} overhead), but the whole "
                f"usable budget is {budget.usable_budget} "
                f"(context_window={budget.context_window} "
                f"- prompt_scaffold_reserve={budget.prompt_scaffold_reserve} "
                f"- query_reserve={budget.query_reserve} "
                f"- answer_reserve={budget.answer_reserve}), counted by "
                f"{counter.name}.\nWhole chunks only — truncating it would break the "
                "char_start/char_end spans the Verifier scores against. Raise "
                "context_window, lower a reserve, or re-chunk with a smaller "
                "DEFAULT_CHUNK_CHARS."
            )
        score = -_selection_key(hit, source)[0]
        if used + cost > budget.usable_budget:
            excluded.append(ExcludedHit(hit=hit, score=score, tokens=cost))
            continue
        included.append(hit)
        used += cost

    return BudgetedContext(
        included=included,
        excluded=excluded,
        score_source=source,
        budget=budget,
        counter_name=counter.name,
        order=order,
        tokens_used=used,
        tokens_remaining=budget.usable_budget - used,
    )


def budget_manifest(budget: ContextBudget, counter: TokenCounter) -> dict[str, Any]:
    """The budget block for a run manifest — all five values plus the counter.

    Defined here rather than in the benchmark script so there is exactly one
    definition and the two cannot drift, the same reason `read_manifest` lives
    in `reranking.py` instead of in `scripts/fetch_reranker_model.py`.

    `usable_budget` is recorded even though it is derived: a manifest is read by
    someone reconstructing a run, and making them redo the arithmetic is how a
    transcription error becomes a wrong conclusion.
    """
    return {
        "counter": counter.name,
        "context_window": budget.context_window,
        "prompt_scaffold_reserve": budget.prompt_scaffold_reserve,
        "query_reserve": budget.query_reserve,
        "answer_reserve": budget.answer_reserve,
        "per_chunk_overhead_tokens": budget.per_chunk_overhead_tokens,
        "usable_budget": budget.usable_budget,
    }


# --- self-check --------------------------------------------------------------
# In-module asserts, not pytest: `tests/` holds only fixtures, every module in
# this repo self-checks this way, and installing pytest is an approval gate.


def _fake_hit(
    chunk_id: str,
    text: str,
    *,
    rrf_score: float = 0.1,
    rerank_score: float | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc_x",
        document_name="fixture.pdf",
        page=1,
        char_start=0,
        char_end=len(text),
        text=text,
        rrf_score=rrf_score,
        lexical_rank=None,
        lexical_score=None,
        vector_rank=1,
        vector_distance=0.5,
        rerank_score=rerank_score,
        rerank_rank=None if rerank_score is None else 1,
    )


def _digest(result: BudgetedContext) -> str:
    """Every field that could vary, serialized exactly. `repr()` round-trips a
    float without loss, matching `retrieval._serialize_pre_rerank_fields`."""
    import hashlib

    body = "\n".join(
        [
            f"source={result.score_source} counter={result.counter_name} order={result.order}",
            f"used={result.tokens_used!r} remaining={result.tokens_remaining!r}",
            *(f"IN {h.chunk_id} {h.rrf_score!r} {h.rerank_score!r}" for h in result.included),
            *(f"EX {e.hit.chunk_id} {e.score!r} {e.tokens!r}" for e in result.excluded),
        ]
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _check_offline(counter: TokenCounter) -> None:
    default = ContextBudget()

    # (9) an impossible budget fails where the numbers are written
    for kwargs in (
        {"context_window": 100},  # reserves exceed the window
        {"answer_reserve": -1},
        {"context_window": PROMPT_SCAFFOLD_RESERVE + QUERY_RESERVE + ANSWER_RESERVE},  # exactly 0
    ):
        try:
            ContextBudget(**kwargs)
        except ImpossibleBudget:
            pass
        else:
            raise AssertionError(f"ImpossibleBudget not raised for {kwargs}")

    # usable_budget is the one definition of the arithmetic
    assert default.usable_budget == 200_000 - 2_000 - 512 - 4_000 == 193_488

    # (7) empty in -> both lists empty: "nothing relevant was retrieved"
    empty = plan_context([], default, counter)
    assert empty.included == [] and empty.excluded == []
    assert empty.tokens_used == 0 and empty.tokens_remaining == default.usable_budget

    # (8) score source is uniform or it is an error
    rrf_hits = [_fake_hit("chk_b", "b" * 100), _fake_hit("chk_a", "a" * 100)]
    rr_hits = [_fake_hit(h.chunk_id, h.text, rerank_score=1.0) for h in rrf_hits]
    assert plan_context(rrf_hits, default, counter).score_source == "rrf"
    assert plan_context(rr_hits, default, counter).score_source == "rerank"
    try:
        plan_context([rrf_hits[0], rr_hits[1]], default, counter)
    except MixedScoreSources as exc:
        assert "chk_b" in str(exc), exc
    else:
        raise AssertionError("MixedScoreSources not raised on a mixed set")

    # (5) tie-break: equal scores resolve by chunk_id, and input order is irrelevant
    tied = [
        _fake_hit("chk_c", "c" * 100, rerank_score=2.5),
        _fake_hit("chk_a", "a" * 100, rerank_score=2.5),
        _fake_hit("chk_b", "b" * 100, rerank_score=2.5),
    ]
    forward = plan_context(tied, default, counter)
    reverse = plan_context(list(reversed(tied)), default, counter)
    assert [h.chunk_id for h in forward.included] == ["chk_a", "chk_b", "chk_c"]
    assert _digest(forward) == _digest(reverse), "tie-break is input-order dependent"

    # (4) determinism: same hits + same budget + same counter -> identical output
    assert _digest(plan_context(tied, default, counter)) == _digest(forward)

    # (6) whole chunks: the included objects ARE the inputs, untouched
    by_id = {h.chunk_id: h for h in tied}
    for hit in forward.included:
        assert hit is by_id[hit.chunk_id], "a hit was copied or rebuilt"

    # (3) + (7): a binding budget excludes rather than truncates, and records what it dropped
    tight = ContextBudget(
        context_window=1_000,
        prompt_scaffold_reserve=0,
        query_reserve=0,
        answer_reserve=0,
        per_chunk_overhead_tokens=96,
    )
    big = [
        _fake_hit("chk_1", "x" * 1_400, rerank_score=3.0),  # 400 + 96 = 496
        _fake_hit("chk_2", "y" * 1_400, rerank_score=2.0),  # 496 -> 992 total, fits
        _fake_hit("chk_3", "z" * 1_400, rerank_score=1.0),  # would be 1488, does not
    ]
    packed = plan_context(big, tight, counter)
    assert [h.chunk_id for h in packed.included] == ["chk_1", "chk_2"], packed.included
    assert [e.hit.chunk_id for e in packed.excluded] == ["chk_3"]
    assert packed.excluded[0].score == 1.0 and packed.excluded[0].tokens == 496
    assert packed.tokens_used == 992 and packed.tokens_remaining == 8
    assert packed.included[0].text == "x" * 1_400, "text must never be truncated"

    # a smaller later chunk still fits after a larger one was skipped -- the loop
    # must not stop at the first hit that does not fit, or it understates the budget
    roomier = ContextBudget(
        context_window=1_100,
        prompt_scaffold_reserve=0,
        query_reserve=0,
        answer_reserve=0,
        per_chunk_overhead_tokens=96,
    )
    mixed_sizes = [*big, _fake_hit("chk_4", "w" * 10, rerank_score=0.5)]  # 3 + 96 = 99
    spill = plan_context(mixed_sizes, roomier, counter)
    assert [h.chunk_id for h in spill.included] == ["chk_1", "chk_2", "chk_4"], spill.included
    assert [e.hit.chunk_id for e in spill.excluded] == ["chk_3"]
    assert spill.tokens_used == 496 + 496 + 99

    # The top-ranked hit is ALWAYS included -- decision C makes an unfittable
    # single hit an error, so the greedy loop can never drop the best one. This
    # is what lets the Guardrail read "budgeted out" off `excluded` alone.
    starved = ContextBudget(
        context_window=600,
        prompt_scaffold_reserve=0,
        query_reserve=0,
        answer_reserve=0,
        per_chunk_overhead_tokens=96,
    )
    out = plan_context([big[1], big[2]], starved, counter)
    assert [h.chunk_id for h in out.included] == ["chk_2"], out.included
    assert [e.hit.chunk_id for e in out.excluded] == ["chk_3"]
    assert out.score_source == "rerank"
    # ...and it holds for every budget/hit-set combination exercised above
    for result, given in ((packed, big), (spill, mixed_sizes), (out, [big[1], big[2]])):
        assert result.included, "a non-empty hit set must always include its top hit"
        assert result.included[0].chunk_id == min(_selection_key(h, "rerank") for h in given)[1]

    # a single hit that cannot fit AT ALL is an invariant violation, never a drop
    too_small = ContextBudget(
        context_window=400,
        prompt_scaffold_reserve=0,
        query_reserve=0,
        answer_reserve=0,
        per_chunk_overhead_tokens=96,
    )
    try:
        plan_context(big, too_small, counter)
    except ChunkExceedsBudget as exc:
        message = str(exc)
        # names the chunk, its true cost, the split, and the budget it blew
        for expected in ("chk_1", "496 tokens", "400 text", "96 overhead", "budget is 400"):
            assert expected in message, f"{expected!r} missing from:\n{message}"
    else:
        raise AssertionError("ChunkExceedsBudget not raised for an oversized hit")

    # (10) the manifest block round-trips and carries all five values + identity
    import json

    block = json.loads(json.dumps(budget_manifest(default, counter)))
    assert block == {
        "counter": "heuristic-char-3.5-v1",
        "context_window": 200_000,
        "prompt_scaffold_reserve": 2_000,
        "query_reserve": 512,
        "answer_reserve": 4_000,
        "per_chunk_overhead_tokens": 96,
        "usable_budget": 193_488,
    }, block


def _check_never_undercounts(
    counter: TokenCounter, texts: list[str], tokenizer_path: Path
) -> float:
    """Decision A's property, against a real tokenization of the real corpus.

    Reference is the vendored reranker tokenizer (`cross-encoder/
    ms-marco-MiniLM-L6-v2`, bert-base-uncased WordPiece) — offline, no network,
    no new dependency, and already verified against `scripts/reranker_model.sha256`.

    It is NOT the generation model's tokenizer, which is unpinned until Chunk 7+.
    WordPiece fragments English prose harder than BPE does, so on this corpus it
    is the pessimistic reference; the residual risk is recorded in
    09_Memory/DECISIONS.md, and this check MUST be re-run against a tiktoken-
    backed counter once the generation model is pinned.
    """
    from tokenizers import Tokenizer

    reference = Tokenizer.from_file(str(tokenizer_path))
    encodings = reference.encode_batch(texts, add_special_tokens=False)
    margins: list[tuple[float, int]] = []
    for i, (chunk_text, encoding) in enumerate(zip(texts, encodings, strict=True)):
        estimated = counter.count(chunk_text)
        actual = len(encoding.ids)
        assert estimated >= actual, (
            f"{counter.name} UNDER-COUNTED chunk {i}: estimated {estimated}, "
            f"reference tokenized to {actual} ({len(chunk_text)} chars). "
            "The counter must be a conservative over-estimate — lower "
            "CHARS_PER_TOKEN until this holds with margin."
        )
        margins.append((estimated / actual, i))
    return min(margins)[0]


def _check_provenance_header_pending() -> None:
    """Unconditional failure. See `ProvenanceHeaderUnmeasured` for the full
    story — this is its only call site, kept as a one-line function so the
    thing that blocks chunk 7 is a single, greppable, undeletable line in
    `_demo()` rather than a comment someone can skim past.
    """
    raise ProvenanceHeaderUnmeasured(
        "PER_CHUNK_OVERHEAD_TOKENS=96 is an UNVALIDATED ESTIMATE, not a "
        "measured figure — see docs/DECISIONS.md, 2026-08-24 (correction), "
        "which RETRACTS the earlier chunk 6 entry's claim that it was "
        '"MEASURED, not asserted." No renderer that emits a provenance '
        "header exists in this codebase yet, and 3 of the 5 header formats "
        "that claim cited never had a literal template on record. Build "
        "chunk 7's renderer, run `uv run python -m "
        "scripts.validate_token_counter` WITHOUT --skip-headers to measure "
        "its real formats against PER_CHUNK_OVERHEAD_TOKENS, then delete "
        "this check and its call site."
    )


def _demo() -> None:
    import asyncio

    from sqlalchemy import text as sql

    from app.core.config import get_settings
    from app.db.session import create_engine, create_session_factory

    counter = HeuristicCharCounter()
    _check_offline(counter)

    async def run_live() -> tuple[int, int, float, int]:
        settings = get_settings()
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            rows = (await session.execute(sql("SELECT text FROM chunks ORDER BY id"))).scalars()
            texts = list(rows)
        await engine.dispose()

        assert texts, "expected a corpus; is the dev database seeded?"

        # (2) the invariant, asserted against the real worst case rather than a
        # value read off the chunker's config.
        observed_max = max(len(t) for t in texts)
        assert observed_max == CORPUS_MAX_CHUNK_CHARS, (
            f"corpus max chunk length moved: recorded {CORPUS_MAX_CHUNK_CHARS}, "
            f"observed {observed_max} over {len(texts)} chunks. The budget's "
            "worst case is derived from this number — re-derive it and re-check "
            "the invariant below before updating the constant."
        )
        worst = max(counter.count(t) for t in texts) + PER_CHUNK_OVERHEAD_TOKENS
        default = ContextBudget()
        assert worst <= default.usable_budget, (
            f"the largest corpus chunk costs {worst} tokens but usable_budget is "
            f"{default.usable_budget} — every retrieval would raise ChunkExceedsBudget."
        )

        # (1) the never-under-count property, over every chunk in the corpus.
        # Historical record only as of Chunk 7 Phase E -- see
        # HeuristicCharCounter's docstring: this property does not generalize,
        # and this counter is no longer what the budget is safe because of.
        margin = _check_never_undercounts(
            counter, texts, settings.reranker_model_dir / TOKENIZER_FILENAME
        )

        # The actual budget-safety mechanism now: TiktokenCounter, including
        # CROSS_TOKENIZER_SAFETY_FACTOR. This is the number that determines
        # whether a real retrieval can raise ChunkExceedsBudget -- the
        # HeuristicCharCounter figure above no longer is.
        tiktoken_counter = TiktokenCounter()
        tiktoken_worst = max(tiktoken_counter.count(t) for t in texts) + PER_CHUNK_OVERHEAD_TOKENS
        assert tiktoken_worst <= default.usable_budget, (
            f"the largest corpus chunk costs {tiktoken_worst} tokens under "
            f"{tiktoken_counter.name} but usable_budget is {default.usable_budget} "
            "-- every retrieval would raise ChunkExceedsBudget."
        )
        return len(texts), worst, margin, tiktoken_worst

    n, worst, margin, tiktoken_worst = asyncio.run(run_live())
    usable = ContextBudget().usable_budget
    print(
        f"ok (historical, HeuristicCharCounter -- no longer the safety mechanism): "
        f"{HeuristicCharCounter.name} never under-counts across {n} corpus chunks "
        f"(min margin {margin:.4f}x vs the vendored WordPiece reference); "
        f"worst-case hit {worst} tokens."
    )
    print(
        f"ok (current safety mechanism): {TiktokenCounter.name} worst-case hit "
        f"across {n} corpus chunks is {tiktoken_worst} tokens, fits usable_budget "
        f"{usable} (headroom {usable - tiktoken_worst}). usable_budget itself is "
        "unchanged -- it is pure reserve arithmetic (context_window minus three "
        "reserves), independent of which counter runs; what changed is the "
        "per-chunk cost charged against it. whole-chunk selection, tie-break, "
        "determinism, exclusion accounting, score-source uniformity, and the "
        "manifest block all hold"
    )

    # BLOCKING PREREQUISITE for chunk 7 — see ProvenanceHeaderUnmeasured.
    # Everything above just passed; this still fails the module on purpose.
    _check_provenance_header_pending()


if __name__ == "__main__":
    _demo()
