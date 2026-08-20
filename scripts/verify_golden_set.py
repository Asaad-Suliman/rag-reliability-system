"""Verify a golden set against the live corpus. Read-only; issues SELECTs only.

    uv run python scripts/verify_golden_set.py [path]   # default: tests/fixtures/golden_set_v3.json

Anchoring contract (see docs/PLAN-step03-chunk2-golden-set-v3.md §0): offsets are
absolute positions into the document's canonical text, but the canonical text no
longer exists anywhere — the source PDF is gone and `documents` stores no text.
They are therefore resolved through the chunk that contains them, which is exact:
`chunking.py` guarantees `canonical_text[c.char_start:c.char_end] == c.text`, so
`c.text[start - c.char_start : end - c.char_start]` is the canonical slice.

Every failure is collected and printed. The exit code is 1 if any check failed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Row, text

from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory

DEFAULT_PATH = Path("tests/fixtures/golden_set_v3.json")


def _check_span(
    entry_id: str,
    label: str,
    span: dict[str, Any],
    chunks: Sequence[Row[Any]],
    failures: list[str],
) -> None:
    """Offsets must fall inside exactly one chunk, and the text there must equal
    `snippet` byte for byte. No normalisation — whitespace differences are drift.
    """
    start, end, snippet = span["char_start"], span["char_end"], span["snippet"]

    if end <= start:
        failures.append(f"{entry_id}: {label} char_end ({end}) is not after char_start ({start})")
        return

    owners = [c for c in chunks if c.char_start <= start and c.char_end >= end]
    if not owners:
        straddled = [c for c in chunks if c.char_start < end and c.char_end > start]
        detail = (
            f"span crosses {len(straddled)} chunk boundaries (chunks do not tile the "
            "canonical text — 246 of 259 consecutive pairs have gaps)"
            if straddled
            else "no chunk covers this range at all"
        )
        failures.append(f"{entry_id}: {label} [{start},{end}) does not resolve — {detail}")
        return

    owner = owners[0]
    extracted = owner.text[start - owner.char_start : end - owner.char_start]
    if extracted != snippet:
        failures.append(
            f"{entry_id}: {label} text at [{start},{end}) does not match snippet\n"
            f"      expected: {snippet!r}\n"
            f"      found:    {extracted!r}"
        )


async def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    if not path.exists():
        print(f"FAIL: {path} does not exist", file=sys.stderr)
        return 1

    golden = json.loads(path.read_text())
    entries = golden["entries"]
    failures: list[str] = []

    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            known_docs = {
                r[0] for r in (await session.execute(text("SELECT id FROM documents"))).all()
            }
            doc_ids = {e["document_id"] for e in entries}
            chunks_by_doc = {
                d: (
                    await session.execute(
                        text(
                            "SELECT char_start, char_end, text FROM chunks "
                            "WHERE document_id = :d ORDER BY char_start"
                        ),
                        {"d": d},
                    )
                ).all()
                for d in doc_ids
                if d in known_docs
            }

            seen_ids: set[str] = set()
            for entry in entries:
                eid = entry.get("id", "<no id>")
                if eid in seen_ids:
                    failures.append(f"{eid}: duplicate entry id")
                seen_ids.add(eid)

                if "answerable" not in entry:
                    failures.append(f"{eid}: missing required `answerable` field")
                    continue

                doc = entry["document_id"]
                if doc not in known_docs:
                    failures.append(f"{eid}: unknown document_id {doc!r}")
                    continue
                chunks = chunks_by_doc[doc]

                if entry["answerable"]:
                    # Must carry its own span, and must NOT carry near-miss fields.
                    for forbidden in ("near_miss_to", "why_unanswerable"):
                        if forbidden in entry:
                            failures.append(f"{eid}: answerable entry must not carry `{forbidden}`")
                    missing = [k for k in ("char_start", "char_end", "snippet") if k not in entry]
                    if missing:
                        failures.append(f"{eid}: answerable entry missing {', '.join(missing)}")
                    else:
                        _check_span(eid, "evidence", entry, chunks, failures)
                else:
                    for required in ("near_miss_to", "why_unanswerable"):
                        if required not in entry:
                            failures.append(f"{eid}: unanswerable entry must carry `{required}`")
                    if not str(entry.get("why_unanswerable", "")).strip():
                        failures.append(f"{eid}: `why_unanswerable` is empty")
                    if "near_miss_to" in entry:
                        _check_span(eid, "near_miss_to", entry["near_miss_to"], chunks, failures)
    finally:
        await engine.dispose()

    if failures:
        print(f"FAIL: {len(failures)} problem(s) in {path}\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    answerable = sum(1 for e in entries if e["answerable"])
    print(
        f"OK: {len(entries)} entries in {path} "
        f"({answerable} answerable, {len(entries) - answerable} near-miss) — "
        "all offsets resolve and every snippet matches exactly"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
