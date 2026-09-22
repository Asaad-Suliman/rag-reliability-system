"""Corpus chunk text for the tests that need it.

The text is third-party book text kept in a gitignored local file (see
`app/services/vector_store.py`). A clone without it must SKIP these tests, never
error: `unittest.SkipTest` is honoured by pytest, and each module's `main()`
catches it and prints `SKIP:` under `python -m`.
"""

from __future__ import annotations

import unittest

from app.services.vector_store import CORPUS_DIR, CORPUS_TEXT_MISSING, _Corpus, _load_corpus


def load_corpus_with_text() -> tuple[_Corpus, list[str]]:
    corpus = _load_corpus(CORPUS_DIR)
    if corpus.documents is None:
        raise unittest.SkipTest(CORPUS_TEXT_MISSING)
    return corpus, corpus.documents
