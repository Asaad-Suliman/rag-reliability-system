"""Parsing entry point: sniff → safety guards → dispatch to a format parser."""

from __future__ import annotations

from pathlib import Path

from app.services.parsing.base import ParsedDocument, Parser
from app.services.parsing.docx import DocxParser
from app.services.parsing.pdf import PdfParser
from app.services.parsing.sniff import (
    DOCX_MIME,
    check_not_empty,
    check_size,
    check_zip_safety,
    sniff_mime_type,
)
from app.services.parsing.text import TextParser

_PARSERS: dict[str, Parser] = {
    "application/pdf": PdfParser(),
    "text/plain": TextParser(),
    "text/markdown": TextParser(),
    DOCX_MIME: DocxParser(),
}


def parse_file(path: Path) -> tuple[str, ParsedDocument]:
    """Full pre-parse pipeline: emptiness, size, sniffing, zip safety, then parse.

    Returns the sniffed MIME type alongside the parsed pages — the caller
    persists the sniffed type, never a client-supplied one.
    """
    check_not_empty(path)
    check_size(path)
    mime_type = sniff_mime_type(path)
    if mime_type == DOCX_MIME:
        check_zip_safety(path)
    return mime_type, _PARSERS[mime_type].parse(path)
