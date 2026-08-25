"""Validate `HeuristicCharCounter` (`app/services/context_budget.py`) against
tiktoken's `cl100k_base` -- the tokenizer family the eventual generation model
is expected to use, as distinct from the vendored cross-encoder's WordPiece
tokenizer that `_check_never_undercounts` already checks against in that same
module.

Two figures from the 2026-08-24 chunk 6 entry are re-examined here, because
neither is backed by a reproducible measurement anywhere in the repo -- both
exist only as a comment in `context_budget.py` and as prose in the matching
`docs/DECISIONS.md` entry:

  1. "min margin 1.0524x" -- measured there against WordPiece, not tiktoken.
     This script re-measures the SAME property against a different reference
     tokenizer and reports both numbers side by side. It does not attempt to
     reconcile them: different reference tokenizers are not directly
     comparable, and a script that forced them to agree would be hiding the
     difference, not confirming it.

  2. the five provenance-header-format token counts (23/40/49/69/74) sizing
     `PER_CHUNK_OVERHEAD_TOKENS = 96`. No renderer that emits a provenance
     header exists anywhere in this codebase -- `docs/DECISIONS.md` names it
     "chunk 7's renderer", future tense, unbuilt. Of the five recorded
     formats, only two have a literal string on record anywhere
     (`[filename | page N | chars a-b]` and `[doc_id=... page=... chars=a-b]`);
     "markdown block", "XML-ish `<source ...>`", and "JSON line" are named
     but never spelled out. This script reconstructs all five against this
     corpus's actual widest live values and reports the real max -- clearly
     labeled per-format as recorded-literal or reconstructed. It does not
     claim to reproduce the original 96-token derivation, because that
     derivation is not reproducible from the written record for 3 of 5 rows.

Reads the existing `chunks` / `documents` tables, text only. No re-embedding,
no embedding API call, no network. Requires tiktoken to already be installed
and `models/tiktoken/` pre-seeded (`scripts/fetch_tiktoken_cache.py`, run with
`--allow-network` in Phase B) -- fails loudly, never silently skips or falls
back, if either is missing.

    uv run python -m scripts.validate_token_counter
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.services.context_budget import (  # noqa: E402
    PER_CHUNK_OVERHEAD_TOKENS,
    HeuristicCharCounter,
)
from scripts.fetch_tiktoken_cache import DEFAULT_DEST as TIKTOKEN_CACHE_DIR  # noqa: E402

# Recorded by the 2026-08-24 DECISIONS.md entry and mirrored as a comment in
# context_budget.py -- against the WordPiece reference tokenizer, not
# tiktoken. Kept here only for the side-by-side report below, not as a value
# this script tries to reproduce.
RECORDED_MIN_MARGIN_WORDPIECE = 1.0524

# ponytail: newly introduced, not inherited. Chunk 6 (context_budget.py /
# DECISIONS.md 2026-08-24) describes "ID- and number-dense text" only in
# prose -- no operational rule is recorded anywhere in this codebase. A chunk
# counts as ID-dense here when more than 15% of its characters are digits.
# Upgrade path: revisit the threshold if it visibly misclassifies one of the
# worst-offender excerpts this script prints.
ID_DENSE_DIGIT_FRACTION = 0.15

EXCERPT_CHARS = 120


class TiktokenNotInstalled(RuntimeError):
    """tiktoken is not importable -- Phase B (`uv add tiktoken`) has not run.

    Never caught and skipped: a counter validated against a tokenizer that
    was not actually loaded is not validated.
    """


def _load_encoding() -> Any:
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(TIKTOKEN_CACHE_DIR))
    try:
        import tiktoken
    except ImportError as exc:
        raise TiktokenNotInstalled(
            "tiktoken is not installed. Run `uv add tiktoken==<pinned version>` "
            "(Phase B, after approval) and "
            "`uv run python -m scripts.fetch_tiktoken_cache --allow-network "
            "--write-manifest` first -- this script refuses to skip the check "
            "or fall back to the heuristic's own count as a stand-in reference."
        ) from exc
    return tiktoken.get_encoding("cl100k_base")


def _is_id_dense(text: str) -> bool:
    if not text:
        return False
    digits = sum(1 for ch in text if ch.isdigit())
    return digits / len(text) > ID_DENSE_DIGIT_FRACTION


async def _load_chunks() -> list[dict[str, Any]]:
    from sqlalchemy import text as sql

    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        rows = (
            await session.execute(
                sql(
                    "SELECT c.id AS chunk_id, c.text, c.page, c.char_start, "
                    "c.char_end, d.filename "
                    "FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "ORDER BY c.id"
                )
            )
        ).mappings()
        chunks = [dict(row) for row in rows]
    await engine.dispose()
    assert chunks, "expected a corpus; is the dev database seeded?"
    return chunks


def _render_headers(chunks: list[dict[str, Any]]) -> dict[str, str]:
    """Five best-effort reconstructions, built from this corpus's actual
    widest live values -- not the hardcoded example values in DECISIONS.md's
    2026-08-24 entry, so this stays a live measurement rather than a copy of
    a stale one. `char_start` is fixed at 0 to match how that entry measured
    the format ("widest real values"), not a real chunk's actual start.
    """
    widest_page = max(c["page"] for c in chunks)
    widest_end = max(c["char_end"] for c in chunks)
    widest_filename = max((c["filename"] for c in chunks), key=len)
    widest_chunk_id = max((c["chunk_id"] for c in chunks), key=len)
    start = 0

    return {
        "bracket-pipe [recorded literally in DECISIONS.md]": (
            f"[{widest_filename} | page {widest_page} | chars {start}-{widest_end}]"
        ),
        "bracket-kv [recorded literally in DECISIONS.md]": (
            f"[doc_id={widest_chunk_id} page={widest_page} chars={start}-{widest_end}]"
        ),
        "markdown block [RECONSTRUCTED -- no literal string on record]": (
            f"**Source:** {widest_filename}, page {widest_page}\n"
            f"**Chunk:** {widest_chunk_id} (chars {start}-{widest_end})"
        ),
        "XML-ish [RECONSTRUCTED -- no literal string on record]": (
            f'<source chunk_id="{widest_chunk_id}" file="{widest_filename}" '
            f'page="{widest_page}" chars="{start}-{widest_end}"/>'
        ),
        "JSON line [RECONSTRUCTED -- no literal string on record]": (
            f'{{"chunk_id": "{widest_chunk_id}", "file": "{widest_filename}", '
            f'"page": {widest_page}, "chars": [{start}, {widest_end}]}}'
        ),
    }


def _counter_report(chunks: list[dict[str, Any]], encoding: Any) -> float:
    """Prints the counter-validation report. Returns the measured min margin
    so a caller (or DECISIONS.md, by hand) can use the real number rather
    than transcribe it from stdout.
    """
    counter = HeuristicCharCounter()

    # (margin, chunk, heuristic_tokens, reference_tokens)
    margins: list[tuple[float, dict[str, Any], int, int]] = []
    for c in chunks:
        heuristic = counter.count(c["text"])
        reference = len(encoding.encode(c["text"]))
        margins.append((heuristic / reference, c, heuristic, reference))

    values = [m[0] for m in margins]
    under_counts = [m for m in margins if m[0] < 1.0]
    prose = [m for m in margins if not _is_id_dense(m[1]["text"])]
    id_dense = [m for m in margins if _is_id_dense(m[1]["text"])]
    worst = sorted(margins, key=lambda m: m[0])[:3]

    min_margin = min(values)
    median_margin = statistics.median(values)

    print(f"chunks: {len(chunks)}")
    print(f"min margin: {min_margin:.4f}x   median: {median_margin:.4f}x")
    print(f"under-counts (margin < 1.0): {len(under_counts)}")
    if under_counts:
        print(
            "  *** THE HEURISTIC UNDER-COUNTED. This is not a rounding note -- "
            "chunk 6's budget (app/services/context_budget.py) can overflow, and "
            "it changes the guardrail work. ***"
        )
    print(
        f"prose (<= {ID_DENSE_DIGIT_FRACTION:.0%} digits): {len(prose)} chunks"
        + (f", min margin {min(m[0] for m in prose):.4f}x" if prose else "")
    )
    print(
        f"id-dense (> {ID_DENSE_DIGIT_FRACTION:.0%} digits) -- RULE NEWLY "
        f"INTRODUCED by this script, not inherited from chunk 6 (which records "
        f"no operational rule, only descriptive prose): {len(id_dense)} chunks"
        + (f", min margin {min(m[0] for m in id_dense):.4f}x" if id_dense else "")
    )

    print("\nworst 3 offenders:")
    for margin, c, heuristic, reference in worst:
        excerpt = c["text"][:EXCERPT_CHARS].replace("\n", " ")
        print(
            f"  {margin:.4f}x  chunk={c['chunk_id']} heuristic={heuristic} "
            f"reference={reference}\n    {excerpt!r}"
        )

    print(
        f"\nrecorded min margin (WordPiece reference, DECISIONS.md 2026-08-24, "
        f"now RETRACTED as a comparison point): {RECORDED_MIN_MARGIN_WORDPIECE}x\n"
        f"this run's min margin (tiktoken cl100k_base reference): {min_margin:.4f}x\n"
        "different reference tokenizers -- reported side by side, not reconciled. "
        "This run's figure is the finding, whatever it is."
    )

    return min_margin


def _header_report(chunks: list[dict[str, Any]], encoding: Any) -> None:
    """Provenance-header section. Kept separate from `_counter_report` so it
    can be skipped with `--skip-headers` -- Phase A established that the
    renderer this measures does not exist yet (docs/DECISIONS.md 2026-08-24
    correction). Run this once chunk 7's renderer is built.
    """
    print(
        "\nprovenance header renderer: NOT FOUND in this codebase. "
        'DECISIONS.md (2026-08-24) calls it "chunk 7\'s renderer" -- unbuilt. '
        "The five formats below are reconstructed from this corpus's actual "
        "widest live values; only the first two have a literal template on "
        "record anywhere."
    )
    headers = _render_headers(chunks)
    header_counts = {label: len(encoding.encode(text)) for label, text in headers.items()}
    for label, count in header_counts.items():
        print(f"  {count:>4} tokens  {label}")
    actual_max = max(header_counts.values())
    print(
        f"\nactual max: {actual_max} tokens vs "
        f"PER_CHUNK_OVERHEAD_TOKENS={PER_CHUNK_OVERHEAD_TOKENS}"
    )
    assert actual_max <= PER_CHUNK_OVERHEAD_TOKENS, (
        f"reconstructed header max {actual_max} exceeds PER_CHUNK_OVERHEAD_TOKENS "
        f"({PER_CHUNK_OVERHEAD_TOKENS}) -- chunk 7's real renderer could violate "
        "the budget if its format renders this large. This is a reconstruction, "
        "not the real renderer -- treat a failure here as a reason to check the "
        "real one closely once it exists, not as proof it is wrong."
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--skip-headers",
        action="store_true",
        help=(
            "counter validation only -- skip the provenance-header "
            "reconstruction. Use until chunk 7's renderer exists for real "
            "(see docs/DECISIONS.md 2026-08-24 correction)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        encoding = _load_encoding()
    except TiktokenNotInstalled as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    chunks = asyncio.run(_load_chunks())
    _counter_report(chunks, encoding)

    if not args.skip_headers:
        _header_report(chunks, encoding)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
