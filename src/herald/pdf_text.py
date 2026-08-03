"""PDF text extraction for the schools corpus (PyMuPDF).

Born-digital board documents extract cleanly; a scanned PDF comes back
(near-)empty and the caller records it as ``no_text`` rather than
chunking garbage. OCR for scanned documents is a later, separate concern.

Two extractors:

* ``extract_pdf_text`` — plain text of every page (the OCR path still uses
  this shape).
* ``extract_pdf`` — table-aware: detect real grids (salary schedules, budgets,
  stipend appendices) with PyMuPDF's ``find_tables`` and pull each one out as a
  whole ``TableBlock`` (markdown), returning the *prose* separately with the
  table regions removed. Keeping a table intact means retrieval finds the whole
  grid and the structured-extraction pass (docs/STRUCTURED.md) gets clean,
  header-bearing input instead of a grid smeared across prose chunks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

# A "table" worth pulling out has at least this many rows and columns; smaller
# find_tables hits are usually a two-column key/value block or a stray ruling,
# which read fine as prose.
_MIN_TABLE_ROWS = 2
_MIN_TABLE_COLS = 2
# Fraction of a text block that must sit inside a table's bbox before we treat
# the block as belonging to the table (and drop it from the prose stream).
_COVER_FRACTION = 0.5


@dataclass(frozen=True)
class ExtractedText:
    text: str
    page_count: int


@dataclass(frozen=True)
class TableBlock:
    """One detected table, kept whole. ``page`` is 1-based."""

    page: int
    markdown: str


@dataclass(frozen=True)
class ExtractedDoc:
    """Table-aware extraction: prose with table regions removed, plus the
    tables themselves as whole markdown blocks."""

    text: str
    tables: list[TableBlock]
    page_count: int

    @property
    def content_chars(self) -> int:
        """Total recovered characters — prose plus every table's markdown.

        A born-digital PDF that is *all* table (a salary-schedule appendix)
        has little prose, so the no-text gate must count table content too or
        it would be misfiled as scanned/empty."""
        return len(self.text) + sum(len(t.markdown) for t in self.tables)


def sanitize(text: str) -> str:
    """Strip NUL (0x00) bytes: PyMuPDF occasionally emits them and
    PostgreSQL text columns reject them (``DataError``). Nothing
    downstream needs them."""
    return text.replace("\x00", "")


def extract_pdf_text(path: str | Path) -> ExtractedText:
    """Plain text of every page, joined with newlines.

    Raises whatever PyMuPDF raises on a broken/encrypted file — the
    ingest loop catches per-document and records the error.
    """
    with fitz.open(str(path)) as doc:
        pages = [page.get_text("text") for page in doc]
    return ExtractedText(text=sanitize("\n".join(pages).strip()), page_count=len(pages))


def _covered(block: fitz.Rect, tables: list[fitz.Rect]) -> bool:
    """True if a text block sits mostly inside one of the table bboxes."""
    area = block.get_area()
    if area <= 0:
        return False
    return any((block & t).get_area() / area >= _COVER_FRACTION for t in tables)


def extract_pdf(path: str | Path) -> ExtractedDoc:
    """Table-aware extraction.

    Per page: find real grids; emit each as a whole ``TableBlock`` (markdown)
    and strip its region from the prose so a salary grid doesn't smear across
    prose chunks. Pages with no table extract exactly as ``get_text("text")``,
    so prose-only documents are byte-for-byte what they were before.

    ``find_tables``/``to_markdown`` are guarded — a detection failure on one
    page degrades to plain-text for that page rather than losing the document.
    """
    prose_pages: list[str] = []
    tables: list[TableBlock] = []
    page_count = 0
    with fitz.open(str(path)) as doc:
        for pno, page in enumerate(doc, start=1):
            page_count += 1
            try:
                found = list(page.find_tables().tables)
            except Exception:
                found = []
            good = [
                t for t in found
                if t.row_count >= _MIN_TABLE_ROWS and t.col_count >= _MIN_TABLE_COLS
            ]
            if not good:
                prose_pages.append(page.get_text("text"))
                continue
            boxes = [fitz.Rect(t.bbox) for t in good]
            kept = [
                block[4]
                for block in page.get_text("blocks")
                if not _covered(fitz.Rect(block[:4]), boxes)
            ]
            prose_pages.append("\n".join(kept))
            for t in good:
                try:
                    md = t.to_markdown().strip()
                except Exception:
                    md = ""
                if md:
                    tables.append(TableBlock(page=pno, markdown=md))
    return ExtractedDoc(
        text=sanitize("\n".join(prose_pages).strip()),
        tables=[TableBlock(page=t.page, markdown=sanitize(t.markdown)) for t in tables],
        page_count=page_count,
    )
