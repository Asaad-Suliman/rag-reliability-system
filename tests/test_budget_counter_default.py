"""`plan_context`'s `counter=None` default branch — the one nothing exercised.

    uv run --no-sync python -m tests.test_budget_counter_default

Plain asserts, no framework: pytest is not installed in this project and every
other test in this directory runs the same way.

Until this file existed, `app/services/context_budget.py`'s
`if counter is None: counter = TiktokenCounter()` had no exerciser anywhere in
the tree. Every call site passed a counter explicitly — the eleven inside
`_check_offline`, and `scripts/chunk7_scores.py`, which says so in as many
words: "the counter is passed explicitly rather than relying on the default, so
a measurement run does not become the first real exercise of the dormant
TiktokenCounter default path." That comment names the gap and declines to be
the thing that fills it. This is the thing that fills it.

Two properties, and the second is the one with teeth:

  1. the default branch is actually taken, and yields `TiktokenCounter`;
  2. when the tiktoken cache is absent, the default branch raises
     `TiktokenCacheMissing` — a deployment defect surfacing here rather than on
     a live query.

**(2) runs in a subprocess, and must.** `tiktoken.get_encoding` memoises into a
module-global `ENCODINGS` dict, so once any `TiktokenCounter()` has succeeded in
a process, a later one returns the memoised encoding, attempts no network call,
and cannot raise. An in-process cold-cache check written after any successful
load would pass vacuously — it would assert nothing and still look green. Same
reasoning as `tests/test_vector_search_stability.py`'s separate OS processes.

The child gets a cold cache by running with its cwd set to an empty directory:
`TIKTOKEN_CACHE_DIR` is `Path("models/tiktoken")`, a RELATIVE path, so a
different cwd is a missing cache. That is not a contrivance — it is exactly the
production failure it stands in for: an image or a working directory that does
not carry `models/tiktoken`.

(1) needs the cache, which is gitignored and local-only (like the corpus text
in `tests/corpus_text.py`). A clone without it SKIPs (1) rather than erroring;
(2) needs no cache and always runs. Only an ABSENT or EMPTY cache directory
skips: a directory that exists but fails to load is a real defect and still
fails.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.services.context_budget import (
    TIKTOKEN_CACHE_DIR,
    ContextBudget,
    HeuristicCharCounter,
    TiktokenCounter,
    plan_context,
)
from app.services.retrieval import RetrievedChunk

REPO_ROOT = Path(__file__).resolve().parents[1]

CACHE_MISSING = (
    f"tiktoken cache {TIKTOKEN_CACHE_DIR} is absent or empty (gitignored, local-only); "
    "populate it with `uv run python -m scripts.fetch_tiktoken_cache --allow-network "
    "--write-manifest`"
)


def _require_tiktoken_cache() -> None:
    """SkipTest when the local cache directory is absent or empty.

    Resolved against the cwd exactly as `TiktokenCounter` resolves it, so this
    checks the same directory the default branch will load from. A directory
    that exists with files in it is NOT skipped: if that fails to load, it is a
    stale or non-matching cache, which is the defect this suite exists to catch.
    """
    cache = TIKTOKEN_CACHE_DIR
    if not cache.is_dir() or not any(cache.iterdir()):
        raise unittest.SkipTest(CACHE_MISSING)


def _hit(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc_x",
        document_name="fixture.pdf",
        page=1,
        char_start=0,
        char_end=len(text),
        text=text,
        rrf_score=0.1,
        lexical_rank=None,
        lexical_score=None,
        vector_rank=1,
        vector_distance=0.5,
    )


# Run in the child. Kept as source text rather than a helper module so the
# child's whole behaviour is readable at the one place it is reasoned about.
_COLD_CACHE_CHILD = """
import sys
sys.path.insert(0, {repo!r})
from app.services.context_budget import ContextBudget, TiktokenCacheMissing, plan_context
from app.services.retrieval import RetrievedChunk

hit = RetrievedChunk(
    chunk_id="chk_1", document_id="doc_x", document_name="fixture.pdf", page=1,
    char_start=0, char_end=5, text="hello", rrf_score=0.1, lexical_rank=None,
    lexical_score=None, vector_rank=1, vector_distance=0.5,
)
try:
    # No counter -> the default branch constructs TiktokenCounter, which must
    # find no cache here and refuse rather than reach the network.
    plan_context([hit], ContextBudget())
except TiktokenCacheMissing as exc:
    print("RAISED", str(exc)[:60])
    sys.exit(0)
print("NO RAISE -- the default branch loaded an encoding from a cold cache")
sys.exit(1)
"""


def test_default_branch_is_taken() -> None:
    """`counter=None` reaches :549-550 and produces TiktokenCounter, not the
    demoted heuristic. `counter_name` is the proof: `plan_context` records the
    counter that actually ran, so this cannot pass by accident.
    """
    _require_tiktoken_cache()
    result = plan_context([_hit("chk_1", "hello world")], ContextBudget())
    assert result.counter_name == TiktokenCounter.name, result.counter_name
    assert result.counter_name != HeuristicCharCounter.name
    assert result.included and result.included[0].chunk_id == "chk_1"
    # An explicit counter still wins -- the default must not override a caller.
    explicit = plan_context([_hit("chk_1", "hello world")], ContextBudget(), HeuristicCharCounter())
    assert explicit.counter_name == HeuristicCharCounter.name
    print(f"ok: counter=None took the default branch -> {result.counter_name}")


def test_cold_cache_raises_from_the_default_branch() -> None:
    """A missing tiktoken cache must surface as `TiktokenCacheMissing` from the
    default branch, never as a network fetch and never as a silent downgrade to
    `HeuristicCharCounter`.
    """
    with tempfile.TemporaryDirectory() as cold:
        proc = subprocess.run(
            [sys.executable, "-c", _COLD_CACHE_CHILD.format(repo=str(REPO_ROOT))],
            cwd=cold,  # relative models/tiktoken does not exist here
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert proc.returncode == 0, (
        f"cold-cache child exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert proc.stdout.startswith("RAISED"), proc.stdout
    print(f"ok: cold cache -> {proc.stdout.strip()}")


def main() -> None:
    try:
        test_default_branch_is_taken()
        branch = "branch taken"
    except unittest.SkipTest as skip:
        print(f"SKIP: default branch taken — {skip}")
        branch = "branch check SKIPPED (no local cache)"
    test_cold_cache_raises_from_the_default_branch()
    print(
        f"\nok: plan_context's counter=None default — {branch}, "
        "and a cold cache refuses in a subprocess rather than passing vacuously"
    )


if __name__ == "__main__":
    main()
