"""Value types shared across the scrape layer."""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum

from pydantic import BaseModel, Field


class DocType(StrEnum):
    """Coarse artifact taxonomy for the schools corpus.

    Kept deliberately small; the ingest adapter can refine these later. The
    string values are used as on-disk directory names, so keep them
    filesystem-safe.
    """

    minutes = "minutes"
    agenda = "agenda"
    policy = "policy"
    handbook = "handbook"
    transcript = "transcript"
    other = "other"


class ScrapedDoc(BaseModel):
    """A retrievable artifact an adapter has *discovered* but not yet fetched.

    ``source_url`` is the thing the runner will GET. Everything else is
    provenance the adapter already knows and wants preserved in the manifest.
    """

    district: str
    doc_type: DocType
    title: str
    source_url: str
    date: _dt.date | None = None
    meeting_id: str | None = None
    committee: str | None = None
    # Hint for the download filename extension when the URL has none
    # (BoardDocs ``/$file/`` links usually carry the real name already).
    suggested_filename: str | None = None


class ManifestEntry(BaseModel):
    """One downloaded artifact, as recorded (append-only) in the manifest.

    The manifest is the contract handed to the ingest adapter: everything it
    needs to load a document (where the bytes live, what it is, where it came
    from) without re-crawling.
    """

    district: str
    doc_type: DocType
    title: str
    source_url: str
    local_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    content_type: str | None = None
    date: _dt.date | None = None
    meeting_id: str | None = None
    committee: str | None = None
    fetched_at: _dt.datetime
