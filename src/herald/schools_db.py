"""Postgres writers for the schools schema (db/migrations/0001_schools_init.sql).

Same conventions as the inherited ``herald.db``: sync psycopg3, functions
take a cursor so they're trivially mockable, the orchestrator owns
connections and transactions. ``herald.db`` stays as the newspaper-shaped
surface the inherited engine still imports; this module is the schools
corpus's write path.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from herald.db import connect as connect  # re-export: pgvector-registered connection


def connect_raw(url: str) -> psycopg.Connection:
    """Connect without registering the pgvector adapter.

    ``connect()`` registers the ``vector`` type, which requires the
    extension to already exist — a chicken-and-egg problem for ``init-db``,
    whose whole job is to *create* that extension. DDL needs no vector
    adapter, so schema application uses this instead.
    """
    return psycopg.connect(url, autocommit=True, prepare_threshold=None)


@dataclass(frozen=True)
class SchoolChunkRow:
    """Row to insert into ``chunks``. ``embedding`` may be None (embed later)."""

    chunk_index: int
    section_path: str
    section_type: str | None
    heading: str | None
    content: str
    embedding: list[float] | None
    meeting_date: _dt.date | None
    doc_type: str | None
    kind: str = "prose"   # 'prose' | 'table'


@dataclass(frozen=True)
class SalaryScheduleRow:
    """One extracted ``salary_schedule`` cell (docs/STRUCTURED.md §2)."""

    school_year: str
    lane: str
    lane_raw: str
    step: int
    years_service: int | None
    is_longevity: bool
    salary: float
    page: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class StipendScheduleRow:
    """One extracted ``stipend_schedule`` row (docs/STRUCTURED.md §2)."""

    position: str
    position_raw: str
    school_year: str = ""
    category: str | None = None
    tier: str = ""
    amount: float | None = None
    amount_high: float | None = None
    amount_pct: float | None = None
    amount_basis: str | None = None
    page: int | None = None
    notes: str | None = None


def upsert_district(
    cur: psycopg.Cursor,
    *,
    slug: str,
    name: str | None = None,
    website: str | None = None,
    boarddocs_slug: str | None = None,
) -> UUID:
    """Insert-or-update a district by slug; always returns the row's id."""
    cur.execute(
        """
        insert into districts (slug, name, website, boarddocs_slug)
        values (%s, %s, %s, %s)
        on conflict (slug) do update set
            name           = coalesce(excluded.name, districts.name),
            website        = coalesce(excluded.website, districts.website),
            boarddocs_slug = coalesce(excluded.boarddocs_slug, districts.boarddocs_slug)
        returning id
        """,
        (slug, name or slug, website, boarddocs_slug),
    )
    return _one_id(cur)


def find_or_insert_document(
    cur: psycopg.Cursor,
    *,
    district_id: UUID,
    doc_type: str,
    title: str,
    source_url: str,
    sha256: str,
    size_bytes: int | None = None,
    content_type: str | None = None,
    local_path: str | None = None,
    committee: str | None = None,
    meeting_id: str | None = None,
    meeting_date: _dt.date | None = None,
    fetched_at: _dt.datetime | None = None,
) -> tuple[UUID, str]:
    """Insert a document if new; return ``(id, ingest_status)``.

    ``ingest_status`` is ``'pending'`` for a fresh insert, or whatever the
    existing row carries — the caller skips rows already ``'ingested'``
    and retries the rest (their chunks were never committed: chunk insert
    and the ``'ingested'`` mark share one transaction).
    """
    cur.execute(
        """
        insert into documents
            (district_id, doc_type, title, source_url, sha256, size_bytes,
             content_type, local_path, committee, meeting_id, meeting_date, fetched_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (district_id, sha256) do nothing
        returning id, ingest_status
        """,
        (district_id, doc_type, title, source_url, sha256, size_bytes,
         content_type, local_path, committee, meeting_id, meeting_date, fetched_at),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "select id, ingest_status from documents where district_id = %s and sha256 = %s",
            (district_id, sha256),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"document (district={district_id}, sha={sha256[:8]}) vanished")
    return _uuid(row[0]), str(row[1])


def mark_document(
    cur: psycopg.Cursor,
    *,
    document_id: UUID,
    status: str,
    error: str | None = None,
    meeting_date: _dt.date | None = None,
    doc_type: str | None = None,
    page_count: int | None = None,
    text_chars: int | None = None,
) -> None:
    """Record the outcome of ingesting one document (and what ingest learned)."""
    cur.execute(
        """
        update documents set
            ingest_status = %s,
            ingest_error  = %s,
            meeting_date  = coalesce(%s, meeting_date),
            doc_type      = coalesce(%s, doc_type),
            page_count    = coalesce(%s, page_count),
            text_chars    = coalesce(%s, text_chars),
            ingested_at   = case when %s = 'ingested' then now() else ingested_at end
        where id = %s
        """,
        (status, error, meeting_date, doc_type, page_count, text_chars, status, document_id),
    )


def insert_chunks(
    cur: psycopg.Cursor,
    *,
    document_id: UUID,
    district_id: UUID,
    rows: list[SchoolChunkRow],
) -> int:
    """Batch-insert one document's chunks. Returns the row count."""
    if not rows:
        return 0
    params: list[tuple[Any, ...]] = [
        (document_id, r.chunk_index, r.section_path, r.section_type, r.heading,
         r.content, r.embedding, district_id, r.meeting_date, r.doc_type, r.kind)
        for r in rows
    ]
    cur.executemany(
        """
        insert into chunks
            (document_id, chunk_index, section_path, section_type, heading,
             content, embedding, district_id, meeting_date, doc_type, kind)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (document_id, chunk_index) do nothing
        """,
        params,
    )
    return len(params)


def upsert_salary(
    cur: psycopg.Cursor,
    *,
    district_id: UUID,
    document_id: UUID,
    rows: list[SalaryScheduleRow],
) -> int:
    """Upsert salary-schedule rows. Idempotent on (district, year, lane, step)."""
    if not rows:
        return 0
    params = [
        (district_id, document_id, r.page, r.school_year, r.lane, r.lane_raw,
         r.step, r.years_service, r.is_longevity, r.salary, r.notes)
        for r in rows
    ]
    cur.executemany(
        """
        insert into salary_schedule
            (district_id, document_id, page, school_year, lane, lane_raw,
             step, years_service, is_longevity, salary, notes)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (district_id, school_year, lane, step) do update set
            document_id   = excluded.document_id,
            page          = excluded.page,
            lane_raw      = excluded.lane_raw,
            years_service = excluded.years_service,
            is_longevity  = excluded.is_longevity,
            salary        = excluded.salary,
            notes         = excluded.notes
        """,
        params,
    )
    return len(params)


def upsert_stipend(
    cur: psycopg.Cursor,
    *,
    district_id: UUID,
    document_id: UUID,
    rows: list[StipendScheduleRow],
) -> int:
    """Upsert stipend rows. Idempotent on (district, year, position, tier)."""
    if not rows:
        return 0
    params = [
        (district_id, document_id, r.page, r.school_year, r.category, r.position,
         r.position_raw, r.tier, r.amount, r.amount_high, r.amount_pct,
         r.amount_basis, r.notes)
        for r in rows
    ]
    cur.executemany(
        """
        insert into stipend_schedule
            (district_id, document_id, page, school_year, category, position,
             position_raw, tier, amount, amount_high, amount_pct, amount_basis, notes)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (district_id, school_year, position, tier) do update set
            document_id  = excluded.document_id,
            page         = excluded.page,
            category     = excluded.category,
            position_raw = excluded.position_raw,
            amount       = excluded.amount,
            amount_high  = excluded.amount_high,
            amount_pct   = excluded.amount_pct,
            amount_basis = excluded.amount_basis,
            notes        = excluded.notes
        """,
        params,
    )
    return len(params)


def mark_chunk_extracted(cur: psycopg.Cursor, chunk_id: UUID) -> None:
    """Stamp a table chunk as processed by herald-extract (resumability)."""
    cur.execute("update chunks set extracted_at = now() where id = %s", (chunk_id,))


def _one_id(cur: psycopg.Cursor) -> UUID:
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("insert/upsert did not return a row")
    return _uuid(row[0])


def _uuid(v: Any) -> UUID:
    return v if isinstance(v, UUID) else UUID(str(v))
