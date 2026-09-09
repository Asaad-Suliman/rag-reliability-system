"""`app/services/provenance.py` — the renderer's five load-bearing properties.

    uv run --no-sync python -m tests.test_provenance

Plain asserts, no framework: pytest is not installed in this project and the
other tests in this directory run the same way.

What is worth testing here is narrow and specific. The renderer has no
branches to speak of; what it has is a contract with two other places in the
codebase, and each check below pins one end of it:

  * byte-stability, because the citation spans and the context string are only
    comparable if the bytes do not move between runs;
  * the locator, because `(document_id, char_start, char_end)` is the key the
    Verifier will address citations by, and a locator built from the wrong
    fields fails silently — it still looks like a citation;
  * the header format against what the counter measures, because the whole
    point of chunk 8.2 is that `scripts/validate_token_counter.py` and the
    renderer stop being two independent copies of one format.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.provenance import (
    DuplicateLocator,
    render,
    render_block,
    render_header,
)


@dataclass(frozen=True)
class _Chunk:
    """The `ChunkRecord` protocol's fields and nothing else.

    Deliberately not a `RetrievedChunk`: if the renderer ever starts reading a
    field outside the protocol it declares, this fails to construct rather
    than quietly passing on a richer object.
    """

    chunk_id: str
    document_id: str
    document_name: str
    page: int
    char_start: int
    char_end: int
    text: str


def _chars(text: str) -> int:
    """A stand-in counter. Real counters are injected; this one just has to be
    deterministic and monotone so the accounting arithmetic is checkable.
    """
    return len(text)


ONE = _Chunk("chk_a", "doc_1", "handbook.pdf", 3, 100, 140, "alpha body")
TWO = _Chunk("chk_b", "doc_2", "policy.docx", 7, 0, 55, "beta body")


def test_byte_stability() -> None:
    first = render([ONE, TWO], _chars)
    second = render([ONE, TWO], _chars)
    assert first.text == second.text
    assert first.text.encode() == second.text.encode()
    assert list(first.citations) == list(second.citations)
    assert first.citations == second.citations
    # Order is the caller's, not the dict's or a sort's.
    reversed_render = render([TWO, ONE], _chars)
    assert reversed_render.text != first.text
    assert list(reversed_render.citations) == list(reversed(list(first.citations)))
    print("ok: byte-stable across two calls; input order preserved")


def test_empty_input() -> None:
    empty = render([], _chars)
    assert empty.text == ""
    assert empty.citations == {}
    assert empty.overhead_tokens == 0
    print("ok: empty input renders empty, not a stray separator")


def test_single_chunk() -> None:
    one = render([ONE], _chars)
    header = "[doc_id=doc_1 page=3 chars=100-140]"
    assert one.text == f"{header}\nalpha body\n\n"
    (citation,) = one.citations.values()
    assert citation.header == header
    assert citation.chunk_id == "chk_a"
    assert citation.document_name == "handbook.pdf"
    assert citation.text_tokens == _chars("alpha body")
    assert citation.block_tokens == _chars(one.text)
    # Overhead is the wrapper and only the wrapper: header + "\n" + "\n\n".
    assert citation.overhead_tokens == len(header) + 3
    assert one.overhead_tokens == citation.overhead_tokens
    print("ok: single chunk renders one block; overhead is the wrapper alone")


def test_locator() -> None:
    two = render([ONE, TWO], _chars)
    assert list(two.citations) == [("doc_1", 100, 140), ("doc_2", 0, 55)]
    for locator, citation in two.citations.items():
        assert locator == citation.locator
        assert locator == (citation.document_id, citation.char_start, citation.char_end)
        # `page` is carried, never keyed on: it cannot address a span.
        assert citation.page not in locator[1:]  # 3 and 7 are not 100/140/0/55

    # Same document, same span, different chunk id — one of the two citations
    # would have to be dropped, and dropping either makes a citation point at
    # a span the answer was not grounded in.
    clash = _Chunk(
        "chk_c", ONE.document_id, ONE.document_name, 4, ONE.char_start, ONE.char_end, "x"
    )
    try:
        render([ONE, clash], _chars)
    except DuplicateLocator as exc:
        assert "chk_a" in str(exc) and "chk_c" in str(exc)
    else:
        raise AssertionError("duplicate locator was accepted; a citation would be lost")

    # Same span in a *different* document is not a clash.
    other_doc = _Chunk("chk_d", "doc_9", "other.pdf", 4, ONE.char_start, ONE.char_end, "y")
    assert len(render([ONE, other_doc], _chars).citations) == 2
    print("ok: locator is (document_id, char_start, char_end); collisions raise")


def test_header_matches_what_the_counter_measures() -> None:
    """`scripts/validate_token_counter.py` measures per-chunk overhead as
    `count(render_block(render_header(...), ""))`. That is only the real
    overhead if the renderer wraps every chunk with exactly those two calls.
    """
    header = render_header(document_id="doc_1", page=3, char_start=100, char_end=140)
    measured = render_block(header, "")

    one = render([ONE], _chars)
    (citation,) = one.citations.values()
    assert citation.header == header
    # The renderer's block is the measured wrapper with the body in it: same
    # prefix, same trailer, and the only difference is the chunk's own text.
    assert measured == f"{header}\n\n\n"
    assert one.text == f"{header}\n{ONE.text}\n\n"
    assert citation.overhead_tokens == _chars(measured)
    print("ok: measured wrapper is byte-identical to the renderer's own")


def main() -> None:
    test_byte_stability()
    test_empty_input()
    test_single_chunk()
    test_locator()
    test_header_matches_what_the_counter_measures()
    print(
        "\nok: provenance renderer — byte-stability, empty input, single chunk, "
        "locator correctness, header/counter agreement"
    )


if __name__ == "__main__":
    main()
