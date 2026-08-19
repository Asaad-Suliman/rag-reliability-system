"""PDF parsing via pypdf.

A page with no extractable text is normal (a mostly-blank page); a *document*
with no extractable text on any page is an image-only scan — pypdf never runs
OCR, so that case is a defined failure, not a crash or a silent empty answer.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from app.services.parsing.base import ParsedDocument, ParsedPage, ParseError, ParseErrorKind


class PdfParser:
    def parse(self, path: Path) -> ParsedDocument:
        try:
            reader = PdfReader(path)
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise ParseError(ParseErrorKind.ENCRYPTED, "This PDF is password-protected.")
            pages = [
                ParsedPage(page_number=i + 1, text=page.extract_text() or "")
                for i, page in enumerate(reader.pages)
            ]
        except ParseError:
            raise
        except Exception as exc:
            # pypdf raises a variety of low-level errors (PdfReadError,
            # struct.error, IndexError, ...) on malformed input depending on
            # exactly where the corruption is — all of them mean the same
            # thing to the caller: this file could not be read.
            raise ParseError(
                ParseErrorKind.CORRUPT, "The PDF could not be read; it may be corrupt."
            ) from exc

        if not any(p.text.strip() for p in pages):
            raise ParseError(
                ParseErrorKind.NO_TEXT_LAYER, "No text layer found; OCR is not supported."
            )

        return ParsedDocument(pages=pages, pages_are_synthetic=False)


def _demo() -> None:
    import tempfile

    from pypdf import PdfWriter

    parser = PdfParser()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Image-only scan: a real, valid PDF with a page but no text stream.
        blank_path = root / "blank.pdf"
        blank = PdfWriter()
        blank.add_blank_page(width=200, height=200)
        with blank_path.open("wb") as f:
            blank.write(f)
        try:
            parser.parse(blank_path)
            raise AssertionError("expected NO_TEXT_LAYER")
        except ParseError as e1:
            assert e1.kind is ParseErrorKind.NO_TEXT_LAYER

        # Encrypted: a real password-protected PDF.
        encrypted_path = root / "encrypted.pdf"
        encrypted = PdfWriter()
        encrypted.add_blank_page(width=200, height=200)
        encrypted.encrypt("secret")
        with encrypted_path.open("wb") as f:
            encrypted.write(f)
        try:
            parser.parse(encrypted_path)
            raise AssertionError("expected ENCRYPTED")
        except ParseError as e2:
            assert e2.kind is ParseErrorKind.ENCRYPTED

        # Corrupt: garbage bytes.
        corrupt_path = root / "corrupt.pdf"
        corrupt_path.write_bytes(b"not a pdf at all")
        try:
            parser.parse(corrupt_path)
            raise AssertionError("expected CORRUPT")
        except ParseError as e3:
            assert e3.kind is ParseErrorKind.CORRUPT

    print("ok: image-only, encrypted, and corrupt PDF cases all classified correctly")


if __name__ == "__main__":
    _demo()
