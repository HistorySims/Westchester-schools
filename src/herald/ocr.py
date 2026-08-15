"""OCR fallback for scanned PDFs / images.

The ingest pipeline records scanned documents (no text layer) as
``no_text``. This recovers their content. Two engines:

* **tesseract** — rasterize each page with PyMuPDF and run Tesseract
  (CPU, free, no key). Good for prose recovery, but returns flat text
  with no table structure, so a scanned salary grid comes back as a jumble
  of numbers — searchable, but not extractable into ``salary_schedule``.

* **vision** — rasterize each page and have Claude transcribe it to
  Markdown, preserving salary/step grids as Markdown tables. Those table
  blocks become ``kind='table'`` chunks, so ``herald-extract`` reads them
  straight into structured salary/stipend rows. This is the path for the
  scanned teacher CBAs, where the numbers are the whole point.

``fitz.open`` also opens image files (jpeg/png), so image documents that
came back ``no_text`` are handled by the same path.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import fitz  # PyMuPDF

from herald.pdf_text import ExtractedDoc, ExtractedText, TableBlock, sanitize

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = "claude-sonnet-5"
_VISION_MAX_TOKENS = 8000  # a dense page of markdown; under the SDK's 10-min guard

_VISION_PROMPT = (
    "Transcribe this scanned page of a school-district document to Markdown, "
    "in reading order. Render any salary schedule, step grid, or other table "
    "as a GitHub-flavored Markdown pipe table with a header row, copying every "
    "numeric cell EXACTLY as printed (no $, no commas added or removed). Do not "
    "summarize, correct, or add commentary — output only the transcription. If "
    "the page is blank, output nothing."
)


def _render_pages(
    path: str | Path, *, dpi: int, max_pages: int | None
) -> tuple[list[tuple[int, bytes]], int]:
    """Rasterize pages to PNG bytes. Returns (``[(page_no, png)]``, page_count).

    ``page_count`` is the document's true length even when ``max_pages`` caps
    how many are rendered."""
    pages: list[tuple[int, bytes]] = []
    with fitz.open(str(path)) as doc:
        page_count = doc.page_count
        for i, page in enumerate(doc):
            if max_pages is not None and i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            pages.append((i + 1, pix.tobytes("png")))
    return pages, page_count


def ocr_pdf(path: str | Path, *, dpi: int = 300, max_pages: int | None = None) -> ExtractedText:
    """OCR every page of a scanned PDF (or image) with Tesseract.

    Returns flat text (no table structure). Raises whatever PyMuPDF/Tesseract
    raise; the ingest loop catches per-document.
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


def _looks_like_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def split_markdown_tables(md: str, *, page: int) -> tuple[str, list[TableBlock]]:
    """Split a page's Markdown into (prose, table blocks).

    A table is a run of two or more consecutive pipe-rows (``| … | … |``),
    which is how the vision prompt is asked to render grids. Everything else
    is prose. Kept deliberately simple: a stray one-line ``|`` stays in prose.
    """
    lines = md.split("\n")
    prose: list[str] = []
    tables: list[TableBlock] = []
    i = 0
    while i < len(lines):
        if _looks_like_table_row(lines[i]):
            j = i
            while j < len(lines) and _looks_like_table_row(lines[j]):
                j += 1
            if j - i >= 2:
                block = "\n".join(lines[i:j]).strip()
                tables.append(TableBlock(page=page, markdown=block))
                i = j
                continue
        prose.append(lines[i])
        i += 1
    return "\n".join(prose).strip(), tables


def _transcribe_page(client, model: str, png: bytes) -> str:
    b64 = base64.standard_b64encode(png).decode("ascii")
    msg = client.messages.create(
        model=model,
        max_tokens=_VISION_MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": _VISION_PROMPT},
            ],
        }],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def ocr_pdf_vision(
    path: str | Path,
    *,
    client,
    model: str = DEFAULT_VISION_MODEL,
    dpi: int = 200,
    max_pages: int | None = None,
) -> ExtractedDoc:
    """Transcribe a scanned PDF to text + table blocks with Claude vision.

    Each page is rasterized and transcribed to Markdown; salary/step grids
    come back as Markdown tables, which are split out into whole ``TableBlock``s
    so ``herald-extract`` can read them into ``salary_schedule``. Prose becomes
    ordinary text. Raises on API/render errors; the ingest loop catches
    per-document.
    """
    pages, page_count = _render_pages(path, dpi=dpi, max_pages=max_pages)
    prose_parts: list[str] = []
    tables: list[TableBlock] = []
    for page_no, png in pages:
        md = _transcribe_page(client, model, png)
        prose, page_tables = split_markdown_tables(md, page=page_no)
        if prose:
            prose_parts.append(prose)
        tables.extend(page_tables)
    return ExtractedDoc(
        text=sanitize("\n\n".join(prose_parts).strip()),
        tables=tables,
        page_count=page_count,
    )
