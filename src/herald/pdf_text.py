"""PDF text extraction for the schools corpus (PyMuPDF).

Born-digital board documents extract cleanly; a scanned PDF comes back
(near-)empty and the caller records it as ``no_text`` rather than
chunking garbage. OCR for scanned documents is a later, separate concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


@dataclass(frozen=True)
class ExtractedText:
    text: str
    page_count: int


def extract_pdf_text(path: str | Path) -> ExtractedText:
    """Plain text of every page, joined with newlines.

    Raises whatever PyMuPDF raises on a broken/encrypted file — the
    ingest loop catches per-document and records the error.
    """
    with fitz.open(str(path)) as doc:
        pages = [page.get_text("text") for page in doc]
    return ExtractedText(text="\n".join(pages).strip(), page_count=len(pages))
