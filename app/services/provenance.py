"""Renders retrieved chunks into the model-facing context string, and the
citation objects that must agree with it byte for byte.

Two things have to stay in lockstep and this module is the only place both are
written: the text the generator sees, and the citations the Verifier scores
against it. A renderer that emits one format and a citation builder that
assumes another is the failure mode this exists to make impossible — so the
header is produced once, by `render_header`, and everything else (the context
string, the per-chunk token accounting, `scripts/validate_token_counter.py`'s
measurement of the real overhead) is derived from that one function.

**Locator: `(document_id, char_start, char_end)`.** Both retrieval arms
populate all three — the lexical arm projects `c.document_id, c.char_start,
c.char_end` (`retrieval.py:84`) and the vector arm reads the same three out of
the point payload (`retrieval.py:300-304`) — so the byte range is available on
every hit regardless of which arm found it. `page` is carried on the citation
as a human-facing convenience, never as part of the key: pages are not unique
within a document and cannot address a span.

**Header format** is the `bracket-kv` literal, the one of the five formats in
the 2026-08-24 DECISIONS.md entry that both has a literal template on record
and names exactly the locator fields. `document_id` here is the real document
id; `scripts/validate_token_counter.py`'s deleted reconstruction passed a
*chunk* id under the same `doc_id=` label, which measured a wider string than
this renderer emits.

Byte-stability is a hard requirement: identical input produces identical bytes
on every run, in every process. Nothing here reads a clock, a hash seed, a set,
or the environment; iteration is over the caller's sequence in its own order.

No I/O, no retrieval, no ranking, no abstention, no generation. The token
counter is injected — this module never picks or loads a tokenizer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

# The one place the header format is written. `scripts/validate_token_counter.py`
# imports `render_header`/`render_block` rather than reconstructing it, so the
# measurement and the renderer cannot drift.
HEADER_TEMPLATE = "[doc_id={document_id} page={page} chars={char_start}-{char_end}]"

# Locator: (document_id, char_start, char_end).
Locator = tuple[str, int, int]


class ChunkRecord(Protocol):
    """What the renderer needs off a retrieved chunk.

    Structural rather than an import of `RetrievedChunk`: this module must not
    reach into retrieval, and every field below is one both arms already
    populate. `RetrievedChunk` satisfies it as written.
    """

    @property
    def chunk_id(self) -> str: ...
    @property
    def document_id(self) -> str: ...
    @property
    def document_name(self) -> str: ...
    @property
    def page(self) -> int: ...
    @property
    def char_start(self) -> int: ...
    @property
    def char_end(self) -> int: ...
    @property
    def text(self) -> str: ...


class DuplicateLocator(ValueError):
    """Two chunks addressed the same `(document_id, char_start, char_end)`.

    Raised rather than resolved, because both resolutions are wrong: keeping
    the last silently drops evidence the budgeter already paid tokens for, and
    keeping the first makes the citation map disagree with the context string
    the generator actually sees. Either way a citation would point at a span
    the answer was not grounded in.
    """


@dataclass(frozen=True)
class Citation:
    """One chunk's provenance, keyed in `RenderedContext.citations` by `locator`.

    `overhead_tokens` is the derived figure `PER_CHUNK_OVERHEAD_TOKENS` is
    supposed to bound: everything this renderer adds around the chunk body —
    header, newline, block separator — and nothing else.
    """

    locator: Locator
    chunk_id: str
    document_id: str
    document_name: str
    page: int
    char_start: int
    char_end: int
    header: str
    block_tokens: int
    text_tokens: int

    @property
    def overhead_tokens(self) -> int:
        return self.block_tokens - self.text_tokens


@dataclass(frozen=True)
class RenderedContext:
    """The generator's context string and the citations that address it.

    `citations` preserves input order (dicts are insertion-ordered), so the
    nth citation is the nth block of `text`.
    """

    text: str
    citations: dict[Locator, Citation]

    @property
    def overhead_tokens(self) -> int:
        return sum(c.overhead_tokens for c in self.citations.values())


def render_header(*, document_id: str, page: int, char_start: int, char_end: int) -> str:
    """The provenance header for one chunk. The only place the format lives."""
    return HEADER_TEMPLATE.format(
        document_id=document_id, page=page, char_start=char_start, char_end=char_end
    )


def render_block(header: str, text: str) -> str:
    """One chunk's full contribution to the context string, separator included.

    Every block carries its own trailing separator rather than joining with
    one, so a block's byte cost is the same whether it is first, last, or
    alone — which is what makes `block_tokens - text_tokens` a per-chunk
    overhead rather than a figure that depends on position.
    """
    return f"{header}\n{text}\n\n"


def render(chunks: Sequence[ChunkRecord], count_tokens: Callable[[str], int]) -> RenderedContext:
    """Render `chunks`, in the given order, into context text plus citations.

    `count_tokens` is injected: the budgeter's heuristic counter, the
    generation model's tokenizer, and the validation script's tiktoken
    encoding are all valid, and choosing between them is not this module's
    call. It is applied only to strings built here — no I/O, no network.

    Byte-stable: same `chunks`, same bytes, every run.
    """
    parts: list[str] = []
    citations: dict[Locator, Citation] = {}
    for chunk in chunks:
        locator: Locator = (chunk.document_id, chunk.char_start, chunk.char_end)
        if locator in citations:
            raise DuplicateLocator(
                f"two chunks share locator {locator}: "
                f"{citations[locator].chunk_id} and {chunk.chunk_id}"
            )
        header = render_header(
            document_id=chunk.document_id,
            page=chunk.page,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        )
        block = render_block(header, chunk.text)
        parts.append(block)
        citations[locator] = Citation(
            locator=locator,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_name=chunk.document_name,
            page=chunk.page,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            header=header,
            block_tokens=count_tokens(block),
            text_tokens=count_tokens(chunk.text),
        )
    return RenderedContext(text="".join(parts), citations=citations)
