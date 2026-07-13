# ============================================================
# FORK TODO (schoolsengine): schema: rename Paper/Issue/Page -> District/Document; decide collapse vs keep pages
# Inherited from Herald; rewrite for this corpus before use.
# See docs/ENGINE_EXTRACTION.md.
# ============================================================
from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field


class Paper(BaseModel):
    """A newspaper title — one row per Chronicling America LCCN."""

    id: UUID | None = None
    lccn: str
    title: str
    place: str | None = None
    start_year: int | None = None
    end_year: int | None = None


class Issue(BaseModel):
    """A single issue (paper + date + edition)."""

    id: UUID | None = None
    paper_id: UUID
    date_issued: date
    edition: int = 1
    loc_url: str


class Page(BaseModel):
    """A single page within an issue."""

    id: UUID | None = None
    issue_id: UUID
    sequence: int
    image_url: str
    jp2_url: str | None = None
    pdf_url: str | None = None
    ocr_text: str | None = None
    ocr_version: int = 1
    ocr_source: str = "loc"


class Chunk(BaseModel):
    """A retrieval-unit slice of a page's OCR text."""

    id: UUID | None = None
    page_id: UUID
    ocr_version: int
    chunk_index: int
    content: str = Field(min_length=1)
    word_start: int = Field(ge=0)
    word_end: int = Field(ge=0)
    is_current: bool = True
