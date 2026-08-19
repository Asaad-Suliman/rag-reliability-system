"""TXT/MD parsing: read as UTF-8. No real pagination — synthetic pages only."""

from __future__ import annotations

from pathlib import Path

from app.services.parsing.base import (
    ParsedDocument,
    ParseError,
    ParseErrorKind,
    synthetic_paginate,
)


class TextParser:
    def parse(self, path: Path) -> ParsedDocument:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ParseError(
                ParseErrorKind.CORRUPT, "The file could not be read; it may be corrupt."
            ) from exc
        return ParsedDocument(pages=synthetic_paginate(text), pages_are_synthetic=True)


def _demo() -> None:
    import tempfile

    parser = TextParser()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        md = root / "note.md"
        md.write_text("# Title\n\nSome body text.", encoding="utf-8")
        result = parser.parse(md)
        assert result.pages_are_synthetic is True
        assert result.pages[0].text == "# Title\n\nSome body text."

        bad = root / "bad.md"
        bad.write_bytes(b"\xff\xfe not valid utf-8 \x00")
        try:
            parser.parse(bad)
            raise AssertionError("expected CORRUPT")
        except ParseError as e:
            assert e.kind is ParseErrorKind.CORRUPT

    print("ok: text/markdown read and undecodable-bytes case both handled correctly")


if __name__ == "__main__":
    _demo()
