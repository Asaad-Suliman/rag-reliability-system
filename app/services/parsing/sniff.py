"""Content sniffing and pre-parse safety guards.

Files are trusted by their bytes, never by extension or client-supplied
Content-Type — Step 02's security gate requires this explicitly. Every guard
here runs before a single byte reaches pypdf or python-docx.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.services.parsing.base import ParseError, ParseErrorKind

MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # API Contract §3 upload limit

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ALLOWED_MIME_TYPES = frozenset({"application/pdf", "text/plain", "text/markdown", DOCX_MIME})

# Zip-bomb / nested-archive guard for the DOCX container.
_MAX_ZIP_MEMBERS = 1000
_MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_MAX_ZIP_COMPRESSION_RATIO = 100
_ARCHIVE_SUFFIXES = (".zip", ".docx", ".xlsx", ".pptx", ".jar", ".rar", ".7z")

_UNSAFE_STRUCTURE_MESSAGE = "The file's internal structure was rejected by a safety check."


def check_not_empty(path: Path) -> None:
    if path.stat().st_size == 0:
        raise ParseError(ParseErrorKind.EMPTY, "The file is empty.")


def check_size(path: Path, max_bytes: int = MAX_FILE_SIZE_BYTES) -> None:
    if path.stat().st_size > max_bytes:
        raise ParseError(ParseErrorKind.TOO_LARGE, "File exceeds the 25 MB limit.")


def sniff_mime_type(path: Path) -> str:
    """Identify the file by its bytes. Raises `UNSUPPORTED_TYPE` if nothing matches."""
    with path.open("rb") as f:
        header = f.read(8)

    if header.startswith(b"%PDF-"):
        return "application/pdf"

    if header.startswith(b"PK\x03\x04"):
        if _looks_like_docx(path):
            return DOCX_MIME
        raise ParseError(ParseErrorKind.UNSUPPORTED_TYPE, "Unsupported file type.")

    if _looks_like_text(path):
        # Markdown has no magic bytes distinct from plain text, so content
        # sniffing cannot tell them apart — and the text parser reads both
        # identically, so the distinction has no effect on parsing.
        return "text/plain"

    raise ParseError(ParseErrorKind.UNSUPPORTED_TYPE, "Unsupported file type.")


def _looks_like_docx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _looks_like_text(path: Path, sample_bytes: int = 8192) -> bool:
    try:
        with path.open("rb") as f:
            sample = f.read(sample_bytes)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def check_zip_safety(path: Path) -> None:
    """Reject zip bombs and nested archives before any DOCX bytes are extracted."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_MEMBERS:
                raise ParseError(ParseErrorKind.UNSAFE_STRUCTURE, _UNSAFE_STRUCTURE_MESSAGE)

            total_uncompressed = 0
            for info in infos:
                if info.filename.lower().endswith(_ARCHIVE_SUFFIXES):
                    raise ParseError(ParseErrorKind.UNSAFE_STRUCTURE, _UNSAFE_STRUCTURE_MESSAGE)
                total_uncompressed += info.file_size
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > _MAX_ZIP_COMPRESSION_RATIO:
                        raise ParseError(ParseErrorKind.UNSAFE_STRUCTURE, _UNSAFE_STRUCTURE_MESSAGE)
            if total_uncompressed > _MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ParseError(ParseErrorKind.UNSAFE_STRUCTURE, _UNSAFE_STRUCTURE_MESSAGE)
    except zipfile.BadZipFile as exc:
        raise ParseError(
            ParseErrorKind.CORRUPT, "The file could not be read; it may be corrupt."
        ) from exc


def _demo() -> None:
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        empty = root / "empty.txt"
        empty.write_bytes(b"")
        try:
            check_not_empty(empty)
            raise AssertionError("expected EMPTY")
        except ParseError as e1:
            assert e1.kind is ParseErrorKind.EMPTY

        oversized = root / "big.txt"
        oversized.write_bytes(b"x" * 100)
        try:
            check_size(oversized, max_bytes=10)
            raise AssertionError("expected TOO_LARGE")
        except ParseError as e2:
            assert e2.kind is ParseErrorKind.TOO_LARGE

        text_file = root / "note.md"
        text_file.write_text("# hello\n\nworld", encoding="utf-8")
        assert sniff_mime_type(text_file) == "text/plain"

        binary_file = root / "note.md"  # same extension, binary content
        binary_file.write_bytes(b"\x00\x01garbage")
        try:
            sniff_mime_type(binary_file)
            raise AssertionError("expected UNSUPPORTED_TYPE")
        except ParseError as e3:
            assert e3.kind is ParseErrorKind.UNSUPPORTED_TYPE

        pdf_file = root / "doc.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n%%EOF")
        assert sniff_mime_type(pdf_file) == "application/pdf"

        docx_file = root / "doc.docx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<xml/>")
        docx_file.write_bytes(buf.getvalue())
        assert sniff_mime_type(docx_file) == DOCX_MIME
        check_zip_safety(docx_file)  # must not raise — well-formed, small

        bomb = root / "bomb.docx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(_MAX_ZIP_MEMBERS + 1):
                zf.writestr(f"word/f{i}.xml", "x")
        bomb.write_bytes(buf.getvalue())
        try:
            check_zip_safety(bomb)
            raise AssertionError("expected UNSAFE_STRUCTURE (member count)")
        except ParseError as e4:
            assert e4.kind is ParseErrorKind.UNSAFE_STRUCTURE

        nested = root / "nested.docx"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<xml/>")
            zf.writestr("media/evil.zip", "x" * 10)
        nested.write_bytes(buf.getvalue())
        try:
            check_zip_safety(nested)
            raise AssertionError("expected UNSAFE_STRUCTURE (nested archive)")
        except ParseError as e5:
            assert e5.kind is ParseErrorKind.UNSAFE_STRUCTURE

    print("ok: all sniffing and zip-safety cases passed")


if __name__ == "__main__":
    _demo()
