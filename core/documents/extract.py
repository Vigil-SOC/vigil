"""Text out of an attached document, read on this machine.

Markdown, text and HTML are read directly. PDFs, images and Office files go to
LiteParse, which runs in-process (Tesseract OCR bundled; Office types need
LibreOffice) and makes no model or network call. Parses run in its worker pool
under a deadline, so one bad file cannot stall the API.
"""

import asyncio
import tempfile
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List

from fastapi import UploadFile

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
# What `document` on a workflow execute accepts; longer text is condensed first.
MAX_DOCUMENT_CHARS = 65_536
POOL_SIZE = 2
PARSE_TIMEOUT_S = 60.0

TEXT_TYPES = frozenset({".md", ".markdown", ".txt", ".text"})
HTML_TYPES = frozenset({".html", ".htm"})
PARSED_TYPES = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
OFFICE_TYPES = frozenset({".docx", ".pptx", ".xlsx"})
ACCEPTED_TYPES = TEXT_TYPES | HTML_TYPES | PARSED_TYPES | OFFICE_TYPES
ACCEPTED_LABEL = "PDF, DOCX, PPTX, XLSX, PNG, JPG, Markdown, plain text or HTML"


class DocumentRefused(Exception):
    """A document that cannot be used, with the reason to show and the status."""

    def __init__(self, reason: str, status: int = 422):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _wrong_type(suffix: str) -> DocumentRefused:
    return DocumentRefused(
        f"{suffix or 'That'} files cannot be attached. Attach {ACCEPTED_LABEL}.",
        status=415,
    )


@dataclass(frozen=True)
class Extracted:
    text: str
    pages: int


class _TextOfHtml(HTMLParser):
    """Visible text only: tags, scripts and styles are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_text(markup: str) -> str:
    parser = _TextOfHtml()
    parser.feed(markup)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


_parsers: Dict[bool, Any] = {}
_parsers_lock = threading.Lock()

NO_OCR_REASON = (
    "{name} needs OCR, and its language data (eng.traineddata) is not on this "
    "server and could not be downloaded. Put it in a folder and set "
    "TESSDATA_PREFIX to that folder."
)


def _liteparse(ocr: bool):
    """The worker pool for OCR on or off, built on first use."""
    with _parsers_lock:
        if ocr not in _parsers:
            from liteparse import LiteParse

            _parsers[ocr] = LiteParse(
                pool_size=POOL_SIZE if ocr else 1,
                parse_timeout=PARSE_TIMEOUT_S,
                ocr_enabled=ocr,
                quiet=True,
            )
        return _parsers[ocr]


def _parse(path: Path, filename: str) -> Extracted:
    from liteparse import ParseError, ParseTimeoutError

    try:
        try:
            result = _liteparse(True).parse(path)
        except ParseError as exc:
            if "ocr failed" not in str(exc).lower():
                raise
            # LiteParse fetches its OCR language data on first use, so an
            # air-gapped install has none. A page with a text layer needs no OCR.
            result = _liteparse(False).parse(path)
            if not result.text.strip():
                raise DocumentRefused(NO_OCR_REASON.format(name=filename)) from None
    except ParseTimeoutError:
        raise DocumentRefused(
            f"Reading {filename} took longer than {PARSE_TIMEOUT_S:.0f} seconds "
            "and was stopped.",
            status=504,
        ) from None
    except ParseError as exc:
        if "libreoffice" in str(exc).lower():
            raise DocumentRefused(
                f"{filename} is an Office file, and reading it needs LibreOffice, "
                "which is not installed on this server."
            ) from None
        raise DocumentRefused(f"Could not read {filename}: {exc}") from None
    return Extracted(result.text, max(result.num_pages, 1))


def extract_document(path: Path, filename: str) -> Extracted:
    """Blocking. Raises ``DocumentRefused`` for a type, a failure or an empty file."""
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_TYPES or suffix in HTML_TYPES:
        text = path.read_bytes().decode("utf-8", errors="replace")
        extracted = Extracted(_html_text(text) if suffix in HTML_TYPES else text, 1)
    elif suffix in PARSED_TYPES or suffix in OFFICE_TYPES:
        extracted = _parse(path, filename)
    else:
        raise _wrong_type(suffix)
    if not extracted.text.strip():
        raise DocumentRefused(f"No text could be read from {filename}.")
    return extracted


async def spool_upload(file: UploadFile, suffix: str) -> Path:
    """Stream an upload to a temp file, refusing it once it passes the size cap."""
    with tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=suffix) as out:
        path = Path(out.name)
        total = 0
        try:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOCUMENT_BYTES:
                    raise DocumentRefused(
                        f"File too large. The most that can be attached is "
                        f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.",
                        status=413,
                    )
                out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


async def extract_upload(file: UploadFile) -> Extracted:
    """Spool, extract off the event loop, and remove the copy."""
    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED_TYPES:
        raise _wrong_type(suffix)  # before a byte is read
    path = await spool_upload(file, suffix)
    try:
        return await asyncio.to_thread(extract_document, path, filename)
    finally:
        path.unlink(missing_ok=True)
