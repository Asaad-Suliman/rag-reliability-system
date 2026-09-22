"""`_neutralise` and `retrieved_context` tag variants — N1-N3 of the pre-registered predictions.

    uv run --no-sync python -m tests.test_neutralise_variants

Pre-registered in `docs/DECISIONS.md` ("`_neutralise` widened to
`retrieved_context` tag variants — PRE-REGISTERED", 2026-09-18).

  * N1  differential: on every input without a variant (exact tags, 23 fixture
        texts, 2 T7 texts, 260 corpus chunks = 288), the module's `_neutralise`
        is byte-identical to the frozen oracle below.
  * N2  every variant leaves no `VARIANT_PATTERN` match in the module's output.
  * N3  negative control: every variant survives the oracle, so N2 discriminates.

Each prediction is evaluated and reported before the exit status is set, so a
first run against the old code shows N2 failing without hiding N3.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

from app.services import generation
from app.services.generation import CONTEXT_CLOSE, CONTEXT_OPEN
from tests.corpus_text import load_corpus_with_text

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "injection_scanner.json").read_text())

# The test's own copy, independent of the implementation (same bytes as IS-02).
VARIANT_PATTERN = re.compile(r"<\s*/?\s*retrieved_context\s*>", re.IGNORECASE)

VARIANTS = (
    "</RETRIEVED_CONTEXT>",
    "<Retrieved_Context>",
    "< retrieved_context>",
    "</ retrieved_context>",
    "< /retrieved_context >",
    "<retrieved_context   >",
    "<\tretrieved_context>",
    "<\n/retrieved_context>",
)

# P-02b holds the variant `< /RETRIEVED_CONTEXT >`; it is N2's territory, not N1's.
N1_EXCLUDED_FIXTURES = frozenset({"P-02b"})
N1_EXPECTED_INPUTS = 288
ORACLE_SOURCE_SHA256 = "31fce72e5a25f04ec13978a5377245d28e4807fe88c43d8374b70e157d8e13fc"


# FROZEN ORACLE — do not edit. Verbatim copy of app/services/generation.py:105-114 at 42c6499.
def _neutralise(value: str) -> str:
    """Defang both fence tags wherever they appear in untrusted text.

    Escaping the angle brackets keeps the text readable to the model while
    making it impossible for a passage -- or a question -- to close the block
    early and have what follows read as instructions.
    """
    return value.replace(CONTEXT_OPEN, "&lt;retrieved_context&gt;").replace(
        CONTEXT_CLOSE, "&lt;/retrieved_context&gt;"
    )


def _n1_inputs() -> list[tuple[str, str]]:
    inputs = [
        ("exact-open", CONTEXT_OPEN),
        ("exact-close", CONTEXT_CLOSE),
        ("both-in-sentence", f"Text with {CONTEXT_OPEN} and {CONTEXT_CLOSE} inside."),
    ]
    inputs += [
        (f["id"], f["text"])
        for f in FIXTURES["positives"] + FIXTURES["negatives"]
        if f["id"] not in N1_EXCLUDED_FIXTURES
    ]
    t7 = FIXTURES["t7_request"]
    inputs += [("t7.question", t7["question"]), ("t7.chunk_text", t7["chunk_text"])]
    corpus, documents = load_corpus_with_text()
    inputs += list(zip(corpus.ids, documents, strict=True))
    return inputs


def test_n1_differential() -> bool:
    source_sha = hashlib.sha256(inspect.getsource(_neutralise).encode()).hexdigest()
    assert source_sha == ORACLE_SOURCE_SHA256, source_sha
    inputs = _n1_inputs()
    assert len(inputs) == N1_EXPECTED_INPUTS, len(inputs)
    # Input guard: the only pattern matches N1 may contain are the exact tags.
    for label, text in inputs:
        assert all(
            m.group() in (CONTEXT_OPEN, CONTEXT_CLOSE) for m in VARIANT_PATTERN.finditer(text)
        ), label
    differing = [
        label for label, text in inputs if generation._neutralise(text) != _neutralise(text)
    ]
    ok = not differing
    same = len(inputs) - len(differing)
    print(f"N1 {'PASS' if ok else 'FAIL'}: {same}/{len(inputs)} byte-identical to oracle")
    for label in differing:
        print(f"N1 differs: {label}")
    return ok


def _survives(neutralise: Callable[[str], str], variant: str) -> bool:
    return VARIANT_PATTERN.search(neutralise(variant)) is not None


def test_n2_variants_neutralised() -> bool:
    survivors = [v for v in VARIANTS if _survives(generation._neutralise, v)]
    for v in VARIANTS:
        print(f"N2 {'FAIL' if v in survivors else 'PASS'}: {v!r}")
    ok = not survivors
    done = len(VARIANTS) - len(survivors)
    print(f"N2 {'PASS' if ok else 'FAIL'}: {done}/{len(VARIANTS)} variants neutralised")
    return ok


def test_n3_oracle_discriminates() -> bool:
    caught = [v for v in VARIANTS if not _survives(_neutralise, v)]
    ok = not caught
    survived = len(VARIANTS) - len(caught)
    print(f"N3 {'PASS' if ok else 'FAIL'}: {survived}/{len(VARIANTS)} variants survive the oracle")
    for v in caught:
        print(f"N3 oracle neutralised: {v!r}")
    return ok


def main() -> None:
    results: dict[str, bool] = {}
    try:
        results["N1"] = test_n1_differential()
    except unittest.SkipTest as skip:
        print(f"SKIP: N1 — {skip}")
    results["N2"] = test_n2_variants_neutralised()
    results["N3"] = test_n3_oracle_discriminates()
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        print(f"\nFAIL: neutralise_variants — {', '.join(failed)}")
        sys.exit(1)
    if "N1" not in results:
        print("\nok: neutralise_variants — N2, N3 (N1 SKIPPED: no local corpus text)")
        return
    print("\nok: neutralise_variants — N1, N2, N3")


if __name__ == "__main__":
    main()
