"""pytest-only wiring. The test modules also run standalone via `python -m`,
where `main()` passes these values in by hand; this file is never imported then.
"""

from __future__ import annotations

# pytest is not a project dependency (it arrives via `uv run --with pytest`), so mypy
# sees no stubs for it; the ignores stay scoped to this file.
import pytest  # type: ignore[import-not-found, unused-ignore]

from tests.test_injection_scanner import _Chunk, _corpus_chunks


@pytest.fixture(scope="session")  # type: ignore[untyped-decorator, unused-ignore]
def chunks() -> list[_Chunk]:
    # Same loader main() uses, so pytest and `python -m` see identical inputs.
    return _corpus_chunks()
