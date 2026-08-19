"""Parser protocol shared by PDF, DOCX, TXT, and MD.

Real page numbers only exist for PDF. Everything else gets `synthetic_paginate`
— fixed-size text windows — so `chunks.page` always has something to point at.
Whether a document's pages are real or synthetic is derivable from
`documents.mime_type` downstream, so it is not persisted as its own column.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_SYNTHETIC_PAGE_CHARS = 3000


@dataclass(frozen=True)
class ParsedPage:
    page_number: int  # 1-indexed
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[ParsedPage]
    pages_are_synthetic: bool


class Parser(Protocol):
    def parse(self, path: Path) -> ParsedDocument: ...


class ParseErrorKind(StrEnum):
    """Classifies a `ParseError` for ingestion.py's status mapping (Chunk 6).

    `UNSAFE_STRUCTURE` maps to `quarantined` — API Contract §3. Every other
    kind maps to `failed`, with `.message` as the user-safe reason.
    """

    CORRUPT = "corrupt"
    ENCRYPTED = "encrypted"
    EMPTY = "empty"
    NO_TEXT_LAYER = "no_text_layer"
    TOO_LARGE = "too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    UNSAFE_STRUCTURE = "unsafe_structure"


class ParseError(Exception):
    """Raised anywhere in `app.services.parsing`. `message` is always user-safe."""

    def __init__(self, kind: ParseErrorKind, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


def synthetic_paginate(text: str, page_chars: int = _SYNTHETIC_PAGE_CHARS) -> list[ParsedPage]:
    """Split into fixed-size windows for formats with no real pagination.

    A boundary is pushed back to the nearest newline (or, failing that, space)
    so a synthetic page never splits a word.
    """
    if not text:
        return [ParsedPage(page_number=1, text="")]

    pages: list[ParsedPage] = []
    start = 0
    page_number = 1
    length = len(text)
    while start < length:
        end = min(start + page_chars, length)
        if end < length:
            split = text.rfind("\n", start, end)
            if split <= start:
                split = text.rfind(" ", start, end)
            if split > start:
                end = split
        pages.append(ParsedPage(page_number=page_number, text=text[start:end]))
        start = end
        page_number += 1
    return pages


def _demo() -> None:
    text = ("word " * 50) + "\n" + ("line " * 700)  # forces at least two windows
    pages = synthetic_paginate(text, page_chars=100)
    assert len(pages) >= 2, "expected more than one synthetic page"
    reconstructed = "".join(p.text for p in pages)
    assert reconstructed == text, "synthetic pagination must not drop or duplicate characters"
    assert all(not p.text.endswith(("wor", "lin")) for p in pages), "must not split mid-word"

    err = ParseError(ParseErrorKind.EMPTY, "The file is empty.")
    assert err.kind is ParseErrorKind.EMPTY
    assert str(err) == "The file is empty."

    assert synthetic_paginate("") == [ParsedPage(page_number=1, text="")]
    print("ok:", len(pages), "synthetic pages from", len(text), "chars")


if __name__ == "__main__":
    _demo()
