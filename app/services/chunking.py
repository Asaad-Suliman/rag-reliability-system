"""Structure-aware sentence packing chunker.

`char_start`/`char_end` are absolute offsets into the document's **canonical
text** — every page's text, verbatim from the parser, joined by
`PAGE_SEPARATOR` with each page's base offset recorded. Nothing is stripped,
collapsed, or normalized before offsets are computed: a chunk's `text` is
always `canonical_text[char_start:char_end]`, a literal slice, never a string
rebuilt by joining sentence fragments. Step 03's Verifier scores claims
against these spans — if `text` were reconstructed instead of sliced, the
offsets would silently stop mapping back to the source the moment whitespace
differed by one character.

Chunks never cross a page boundary, so `page` is unambiguous for every chunk.
Sizing is character-based, not tokenizer-based: chunks target ~500 tokens
against a 32,000-token embedding context, a 64x margin that absorbs a rough
4-chars-per-token estimate without a tokenizer dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.parsing.base import ParsedDocument

DEFAULT_CHUNK_CHARS = 2000  # ~500 tokens at ~4 chars/token
DEFAULT_OVERLAP_CHARS = 300  # ~15%
PAGE_SEPARATOR = "\n\n"

# A sentence boundary is end punctuation followed by whitespace and a
# capital/digit/quote, or a blank-line paragraph break. The boundary
# whitespace is folded into the END of the preceding span rather than
# dropped, so spans tile the page text with no gaps — packing only ever
# has to slice, never rebuild.
_SENTENCE_BOUNDARY_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'(])|\n\s*\n+')


@dataclass(frozen=True)
class ChunkSpan:
    ordinal: int
    page: int
    char_start: int
    char_end: int
    text: str
    token_estimate: int


@dataclass(frozen=True)
class ChunkedDocument:
    canonical_text: str
    chunks: list[ChunkSpan]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Contiguous spans tiling `text` exactly — concatenating them reconstructs it."""
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        end = m.end()
        spans.append((start, end))
        start = end
    spans.append((start, len(text)))
    return spans


def _hard_split(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Break one oversized span at word boundaries.

    Only reached by a single sentence longer than `max_chars` — rare, but
    must not crash or silently drop text.
    """
    pieces: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        limit = min(pos + max_chars, end)
        if limit < end:
            split = text.rfind(" ", pos, limit)
            split = split + 1 if split > pos else limit
        else:
            split = limit
        pieces.append((pos, split))
        pos = split
    return pieces


def _atomize(page_text: str, chunk_chars: int) -> list[tuple[int, int]]:
    atoms: list[tuple[int, int]] = []
    for start, end in _sentence_spans(page_text):
        if end - start <= chunk_chars:
            atoms.append((start, end))
        else:
            atoms.extend(_hard_split(page_text, start, end, chunk_chars))
    return atoms


def _pack_page(page_text: str, chunk_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Greedy sentence packing within one page. Returns page-local (start, end) spans."""
    atoms = _atomize(page_text, chunk_chars)
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(atoms)
    while i < n:
        chunk_start = atoms[i][0]
        end_idx = i
        while end_idx + 1 < n and atoms[end_idx + 1][1] - chunk_start <= chunk_chars:
            end_idx += 1
        chunk_end = atoms[end_idx][1]
        if page_text[chunk_start:chunk_end].strip():
            spans.append((chunk_start, chunk_end))

        if end_idx == n - 1:
            break

        # Carry trailing atoms whose combined length is <= overlap_chars into
        # the next chunk. `i` always advances by at least one atom so the
        # loop terminates even when overlap_chars is 0 or an atom is oversized.
        overlap_idx = end_idx
        while overlap_idx > i and atoms[end_idx][1] - atoms[overlap_idx - 1][0] <= overlap_chars:
            overlap_idx -= 1
        i = max(overlap_idx, i + 1)
    return spans


def chunk_document(
    document: ParsedDocument,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> ChunkedDocument:
    """Chunk every page independently, then place each span in the document's
    canonical text (pages joined by `PAGE_SEPARATOR`) to get absolute offsets.
    """
    # Built first, from the parser's untouched page text, so every offset
    # below is computed against — and every chunk's text sliced from — this
    # exact string. There is no separate "display" or "storage" copy.
    canonical_text = PAGE_SEPARATOR.join(page.text for page in document.pages)

    chunks: list[ChunkSpan] = []
    ordinal = 0
    base_offset = 0
    for page in document.pages:
        for local_start, local_end in _pack_page(page.text, chunk_chars, overlap_chars):
            abs_start = base_offset + local_start
            abs_end = base_offset + local_end
            text = canonical_text[abs_start:abs_end]
            chunks.append(
                ChunkSpan(
                    ordinal=ordinal,
                    page=page.page_number,
                    char_start=abs_start,
                    char_end=abs_end,
                    text=text,
                    token_estimate=max(1, len(text) // 4),
                )
            )
            ordinal += 1
        base_offset += len(page.text) + len(PAGE_SEPARATOR)

    return ChunkedDocument(canonical_text=canonical_text, chunks=chunks)


def _demo() -> None:
    from app.services.parsing.base import ParsedPage

    # Two pages, deliberately irregular whitespace and a sentence long enough
    # to force a hard split, so both the packing loop and the hard-splitter run.
    page1_text = (
        "Retrieval-augmented generation combines a retriever with a generator. "
        "It grounds answers in retrieved evidence.\n\n"
        "This is a second   paragraph with   irregular spacing that must survive untouched. "
        + ("word " * 500)  # forces a hard split — one "sentence" > chunk_chars
    )
    page2_text = "Groundedness scoring compares claims to retrieved spans. It is not optional."

    doc = ParsedDocument(
        pages=[
            ParsedPage(page_number=1, text=page1_text),
            ParsedPage(page_number=2, text=page2_text),
        ],
        pages_are_synthetic=False,
    )

    result = chunk_document(doc, chunk_chars=300, overlap_chars=50)
    assert len(result.chunks) >= 3, "expected the hard-split sentence to force multiple chunks"

    # The core guarantee: every chunk's text is a verbatim slice of the
    # canonical text, and canonical text is the parser's page text untouched.
    assert result.canonical_text == page1_text + PAGE_SEPARATOR + page2_text
    for chunk in result.chunks:
        assert result.canonical_text[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.text != "", "no chunk should be blank"

    # Page assignment matches which original page the offsets fall in, and no
    # chunk crosses the page boundary.
    page1_end = len(page1_text)
    for chunk in result.chunks:
        if chunk.page == 1:
            assert chunk.char_end <= page1_end
        else:
            assert chunk.char_start >= page1_end + len(PAGE_SEPARATOR)

    # Overlap: consecutive same-page chunks share trailing/leading text.
    same_page = [c for c in result.chunks if c.page == 1]
    if len(same_page) >= 2:
        a, b = same_page[0], same_page[1]
        assert b.char_start < a.char_end, "expected overlap between consecutive chunks"

    print(f"ok: {len(result.chunks)} chunks, every offset round-trips exactly")


if __name__ == "__main__":
    _demo()
