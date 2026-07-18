"""OCR fallback for scanned PDFs / images (Tesseract via PyMuPDF).

The ingest pipeline records scanned documents (no text layer) as
``no_text``. This recovers their text: rasterize each page with PyMuPDF
and run Tesseract. Modern district scans are clean ~300dpi PDFs, so
Tesseract does well — this is not the 1840s-microfilm problem the
newspaper engine fought. Runs entirely on CPU: no API, no key, no
per-page charge (just the ``tesseract-ocr`` binary on the runner).

``fitz.open`` also opens image files (jpeg/png), so image documents that
came back ``no_text`` are handled by the same path.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # PyMuPDF

from herald.pdf_text import ExtractedText, sanitize


def ocr_pdf(path: str | Path, *, dpi: int = 300, max_pages: int | None = None) -> ExtractedText:
    """OCR every page of a scanned PDF (or an image file).

    ``page_count`` is the document's true page count even when
    ``max_pages`` truncates how many are actually OCR'd. Raises whatever
    PyMuPDF/Tesseract raise; the ingest loop catches per-document.
    """
    import pytesseract
    from PIL import Image

    texts: list[str] = []
    with fitz.open(str(path)) as doc:
        page_count = doc.page_count
        for i, page in enumerate(doc):
            if max_pages is not None and i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            texts.append(pytesseract.image_to_string(img))
    return ExtractedText(text=sanitize("\n".join(texts).strip()), page_count=page_count)
