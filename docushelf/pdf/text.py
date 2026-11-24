"""Text extraction from PDFs (text layer only; OCR happens before)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pypdfium2 as pdfium

from . import PDFIUM_LOCK

_TRAILING_SPACES_RE = re.compile(r"[ \t]+\n")
_MANY_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    value = unicodedata.normalize("NFC", text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    value = value.replace("￾", "").replace("￿", "")
    value = _TRAILING_SPACES_RE.sub("\n", value)
    value = _MANY_BLANK_LINES_RE.sub("\n\n", value)
    return value.strip()


def _check_readable(path: Path) -> None:
    """Raise the proper OSError (missing file, no permission) before pdfium reports a vague one."""
    with open(path, "rb"):
        pass


def extract_pages(path: Path, *, max_pages: int | None = None) -> list[str]:
    """Return the text of every page (empty strings for pages without text)."""
    _check_readable(path)
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            count = len(pdf)
            if max_pages is not None:
                count = min(count, max_pages)
            pages: list[str] = []
            for index in range(count):
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        raw = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                pages.append(clean_text(raw))
            return pages
        finally:
            pdf.close()


def page_count(path: Path) -> int:
    _check_readable(path)
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()


def has_text(pages: list[str], *, min_chars: int = 20) -> bool:
    return sum(len(p.strip()) for p in pages) >= min_chars
