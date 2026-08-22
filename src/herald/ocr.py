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
from collections.abc import Sequence
from pathlib import Path

import fitz  # PyMuPDF

from herald.pdf_text import ExtractedDoc, ExtractedText, TableBlock, sanitize

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = "claude-sonnet-5"

# max_tokens caps thinking AND output together, and this model runs adaptive
# thinking by default (omitting `thinking` does NOT mean thinking-off here).
# At the old 8000 a dense rotated salary grid — the hardest page in the corpus,
# and the one we actually care about — spent its budget on thinking and was
# truncated before emitting any table, silently, because nothing checked
# stop_reason. Ordinary prose pages transcribed fine, which is exactly how the
# failure hid. Give transcription real headroom and stream (the SDK refuses
# non-streaming requests it estimates may exceed ~10 minutes).
_VISION_MAX_TOKENS = 32000

_VISION_PROMPT = (
    "Transcribe this scanned page of a school-district document to Markdown, "
    "in reading order. Render any salary schedule, step grid, or other table "
    "as a GitHub-flavored Markdown pipe table with a header row, copying every "
    "numeric cell EXACTLY as printed (no $, no commas added or removed). Do not "
    "summarize, correct, or add commentary — output only the transcription. If "
    "the page is blank, output nothing.\n\n"
    "The page may still be rotated (salary appendices are often landscape "
    "scanned sideways). If so, read it in its correct orientation and still "
    "emit a proper Markdown table — never fall back to prose for a grid. "
    "A wide grid keeps every column: one row per step, one column per "
    "salary lane exactly as headed (e.g. BA, BA15, MA30, DR), preserving "
    "any '(Frozen)' or similar column annotation in the header cell."
)


# The API rejects an image above this size; a 300-dpi tabloid page can exceed it.
_MAX_IMAGE_BYTES = 9_000_000


def upright(img):
    """Rotate a page image upright, using Tesseract's orientation detection.

    Scanned contracts routinely save a landscape salary appendix sideways into
    a portrait page (Tarrytown's Appendix A is exactly this). A rotated dense
    numeric grid is the case vision transcription most reliably fails on — it
    comes back as prose instead of a table — so straightening the page before
    transcription is what makes the grid extractable at all. Best effort: if
    detection is unavailable or unsure, the image is returned unchanged.
    """
    try:
        import pytesseract

        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        angle = int(osd.get("rotate", 0) or 0)
    except Exception as exc:  # no tesseract binary, or OSD found too little text
        logger.debug("orientation detection unavailable: %s", exc)
        return img
    return img.rotate(-angle, expand=True) if angle % 360 else img


def _encode_png(img, *, max_bytes: int = _MAX_IMAGE_BYTES) -> bytes:
    """PNG bytes for an image, downscaled until it fits the API's size cap."""
    data = b""
    for scale in (1.0, 0.75, 0.5, 0.35):
        out = img if scale == 1.0 else img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        )
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    return data


def _render_pages(
    path: str | Path, *, dpi: int, max_pages: int | None,
    only: Sequence[int] | None = None,
) -> tuple[list[tuple[int, bytes]], int]:
    """Rasterize pages to upright, size-capped PNG bytes.

    Returns ``([(page_no, png)], page_count)``; ``page_count`` is the
    document's true length even when ``max_pages`` caps how many are rendered.
    ``only`` restricts rendering to those 1-based page numbers — the whole
    point of page-level OCR is to pay for the four scanned pages in a
    seventy-page document, not the seventy.
    """
    from PIL import Image

    wanted = set(only) if only is not None else None
    pages: list[tuple[int, bytes]] = []
    with fitz.open(str(path)) as doc:
        page_count = doc.page_count
        for i, page in enumerate(doc):
            page_no = i + 1
            if wanted is not None and page_no not in wanted:
                continue
            if max_pages is not None and len(pages) >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            pages.append((page_no, _encode_png(upright(img))))
    return pages, page_count


def ocr_pdf(path: str | Path, *, dpi: int = 300, max_pages: int | None = None,
            pages: Sequence[int] | None = None) -> ExtractedText:
    """OCR every page of a scanned PDF (or image) with Tesseract.

    Returns flat text (no table structure). Raises whatever PyMuPDF/Tesseract
    raise; the ingest loop catches per-document.
    """
    import pytesseract
    from PIL import Image

    wanted = set(pages) if pages is not None else None
    texts: list[str] = []
    done = 0
    with fitz.open(str(path)) as doc:
        page_count = doc.page_count
        for i, page in enumerate(doc):
            if wanted is not None and (i + 1) not in wanted:
                continue
            if max_pages is not None and done >= max_pages:
                break
            done += 1
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


def _transcribe_page(client, model: str, png: bytes, *, page_no: int = 0) -> str:
    b64 = base64.standard_b64encode(png).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": _VISION_PROMPT},
        ],
    }]
    with client.messages.stream(
        model=model, max_tokens=_VISION_MAX_TOKENS, messages=messages,
    ) as stream:
        msg = stream.get_final_message()
    text = "".join(b.text for b in msg.content if b.type == "text")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        # Loud, because the silent version of this cost us the salary grid.
        logger.warning(
            "page %s hit max_tokens (%s) — transcription truncated, %d chars kept",
            page_no, _VISION_MAX_TOKENS, len(text),
        )
    return text


def ocr_pdf_vision(
    path: str | Path,
    *,
    client,
    model: str = DEFAULT_VISION_MODEL,
    dpi: int = 200,
    max_pages: int | None = None,
    pages: Sequence[int] | None = None,
) -> ExtractedDoc:
    """Transcribe a scanned PDF to text + table blocks with Claude vision.

    Each page is rasterized and transcribed to Markdown; salary/step grids
    come back as Markdown tables, which are split out into whole ``TableBlock``s
    so ``herald-extract`` can read them into ``salary_schedule``. Prose becomes
    ordinary text. Raises on API/render errors; the ingest loop catches
    per-document.
    """
    rendered, page_count = _render_pages(path, dpi=dpi, max_pages=max_pages, only=pages)
    prose_parts: list[str] = []
    tables: list[TableBlock] = []
    for page_no, png in rendered:
        md = _transcribe_page(client, model, png, page_no=page_no)
        prose, page_tables = split_markdown_tables(md, page=page_no)
        if prose:
            prose_parts.append(prose)
        tables.extend(page_tables)
    return ExtractedDoc(
        text=sanitize("\n\n".join(prose_parts).strip()),
        tables=tables,
        page_count=page_count,
    )
