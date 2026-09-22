"""`injection_scanner` v1: a deterministic LLM01 tripwire on retrieved context.

Report-only: `app/api/v1/query.py` calls `scan()` on the budgeted chunks and logs
counts and chunk ids; a finding never changes the response.
Every check is a tripwire with unknown recall, not a defence. Pre-registered in
`docs/DECISIONS.md`, 2026-09-18, "`injection_scanner` v1 PRE-REGISTERED";
changing any pattern needs a new DECISIONS entry.

  * IS-01 fake provenance header -- derived at import from
    `provenance.HEADER_TEMPLATE`, so the only hand-written part is the
    field-to-pattern map below.
  * IS-02 `<retrieved_context>` tag attempts -- derived from
    `generation.CONTEXT_OPEN`/`CONTEXT_CLOSE`, case- and whitespace-tolerant.
    Detection only; `_neutralise` still owns defanging.
  * IS-03.1-.4 instruction-override tripwire -- four strings frozen by the
    DECISIONS entry, compiled byte-for-byte.

`scan()` stores offsets and lengths, never matched text: hostile text must not
be copied into logs. No I/O, no logging, no clocks, no mutable globals.
"""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.services.generation import CONTEXT_CLOSE, CONTEXT_OPEN
from app.services.provenance import HEADER_TEMPLATE, ChunkRecord

CheckId = Literal["IS-01", "IS-02", "IS-03.1", "IS-03.2", "IS-03.3", "IS-03.4"]

# IS-01. `render_header` does not validate its inputs, so the int fields admit
# negatives and bools as well as digits. A `document_id` containing `\n` is a
# deliberate gap: ids are ULIDs.
_INT_FIELD = r"(?:-?\d+|True|False)"
_HEADER_FIELD_PATTERNS = {
    "document_id": r"[^\n]*?",
    "page": _INT_FIELD,
    "char_start": _INT_FIELD,
    "char_end": _INT_FIELD,
}


def _header_pattern() -> str:
    parsed = list(string.Formatter().parse(HEADER_TEMPLATE))
    fields = {field for _, field, _, _ in parsed if field is not None}
    # Drift guard: a changed template fails the import instead of silently
    # not matching.
    assert fields == _HEADER_FIELD_PATTERNS.keys(), (fields, _HEADER_FIELD_PATTERNS.keys())
    parts: list[str] = []
    for literal, field, _, _ in parsed:
        parts.append(re.escape(literal))
        if field is not None:
            parts.append(_HEADER_FIELD_PATTERNS[field])
    return "".join(parts)


def _fence_tag_pattern() -> str:
    name = CONTEXT_OPEN[1:-1]
    assert CONTEXT_OPEN == f"<{name}>" and CONTEXT_CLOSE == f"</{name}>", (
        CONTEXT_OPEN,
        CONTEXT_CLOSE,
    )
    return r"<\s*/?\s*" + re.escape(name) + r"\s*>"


# Ordered by check id: `scan()` iterates this order, which is the findings'
# within-chunk sort key.
PATTERNS: dict[CheckId, re.Pattern[str]] = {
    "IS-01": re.compile(_header_pattern()),
    "IS-02": re.compile(_fence_tag_pattern(), re.IGNORECASE),
    # IS-03: FROZEN by the pre-registration entry. Byte-for-byte.
    "IS-03.1": re.compile(
        r"\bignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE
    ),
    "IS-03.2": re.compile(r"\bignore\s+all\s+prior\b", re.IGNORECASE),
    "IS-03.3": re.compile(
        r"\bdisregard\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE
    ),
    # `[ \t]*`, not `\s*`: under MULTILINE a leading `\s*` could start the
    # match on an earlier blank line.
    "IS-03.4": re.compile(r"^[ \t]*(system|assistant)[ \t]*:", re.IGNORECASE | re.MULTILINE),
}


@dataclass(frozen=True, slots=True)
class Finding:
    check_id: CheckId
    chunk_id: str
    span_start: int  # index into chunk.text, half-open [start, end)
    span_end: int
    matched_excerpt_len: int  # == span_end - span_start; the matched text is never stored


@dataclass(frozen=True, slots=True)
class ScanResult:
    findings: tuple[Finding, ...]  # input chunk order, then check_id, then span_start
    chunks_scanned: int
    clean: bool

    def __post_init__(self) -> None:
        if self.clean != (len(self.findings) == 0):
            raise ValueError(f"clean={self.clean} contradicts {len(self.findings)} findings")


def scan(chunks: Sequence[ChunkRecord]) -> ScanResult:
    """Every match of every check in each chunk's raw `text`. Overlaps across
    checks are reported, not deduplicated. Pure and deterministic."""
    findings = tuple(
        Finding(
            check_id=check_id,
            chunk_id=chunk.chunk_id,
            span_start=m.start(),
            span_end=m.end(),
            matched_excerpt_len=m.end() - m.start(),
        )
        for chunk in chunks
        for check_id, pattern in PATTERNS.items()
        for m in pattern.finditer(chunk.text)
    )
    return ScanResult(findings=findings, chunks_scanned=len(chunks), clean=not findings)
