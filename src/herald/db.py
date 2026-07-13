"""Supabase / Postgres writer.

Sync psycopg3 with pgvector type registration. Functions are designed to
take a cursor so they're trivially mockable; the high-level pipeline opens
a connection, registers pgvector, and runs work inside transactions.

See PLAN.md §5 for schema and §10 for the re-OCR write contract (Phase 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector


def connect(url: str) -> psycopg.Connection:
    """Open a Postgres connection and register the pgvector adapter.

    ``prepare_threshold=None`` disables psycopg's automatic prepared
    statements. The Supabase pooler in *transaction* mode does not support
    them; session mode does, but disabling globally costs almost nothing
    for a batch ingest and keeps the code portable across pooler modes.
    """
    conn = psycopg.connect(url, autocommit=False, prepare_threshold=None)
    register_vector(conn)
    return conn


@dataclass(frozen=True)
class ChunkRow:
    """Row to insert into ``chunks``. Embedding is a plain float list."""

    chunk_index: int
    content: str
    word_start: int
    word_end: int
    embedding: list[float]


# ---- papers / issues / pages -----------------------------------------

def upsert_paper(
    cur: psycopg.Cursor,
    *,
    lccn: str,
    title: str,
    place: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
) -> UUID:
    """Insert-or-update a row in ``papers``; always returns the row's id."""
    cur.execute(
        """
        insert into papers (lccn, title, place, start_year, end_year)
        values (%s, %s, %s, %s, %s)
        on conflict (lccn) do update set
            title      = excluded.title,
            place      = coalesce(excluded.place, papers.place),
            start_year = coalesce(excluded.start_year, papers.start_year),
            end_year   = coalesce(excluded.end_year, papers.end_year)
        returning id
        """,
        (lccn, title, place, start_year, end_year),
    )
    return _fetch_one_id(cur)


def upsert_issue(
    cur: psycopg.Cursor,
    *,
    paper_id: UUID,
    date_issued: date,
    edition: int,
    loc_url: str,
) -> UUID:
    """Insert-or-update a row in ``issues``; always returns the row's id."""
    cur.execute(
        """
        insert into issues (paper_id, date_issued, edition, loc_url)
        values (%s, %s, %s, %s)
        on conflict (paper_id, date_issued, edition) do update set
            loc_url = excluded.loc_url
        returning id
        """,
        (paper_id, date_issued, edition, loc_url),
    )
    return _fetch_one_id(cur)


def find_or_insert_page(
    cur: psycopg.Cursor,
    *,
    issue_id: UUID,
    sequence: int,
    image_url: str,
    jp2_url: str | None,
    pdf_url: str | None,
    ocr_text: str | None,
) -> tuple[UUID, bool]:
    """Insert a page if it doesn't already exist.

    Returns ``(page_id, was_new)``. When ``was_new`` is False the caller
    should *not* re-write chunks; the existing page is left alone.
    """
    cur.execute(
        """
        insert into pages (issue_id, sequence, image_url, jp2_url, pdf_url, ocr_text)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (issue_id, sequence) do nothing
        returning id
        """,
        (issue_id, sequence, image_url, jp2_url, pdf_url, ocr_text),
    )
    row = cur.fetchone()
    if row is not None:
        return _coerce_uuid(row[0]), True
    cur.execute(
        "select id from pages where issue_id = %s and sequence = %s",
        (issue_id, sequence),
    )
    row = cur.fetchone()
    if row is None:
        # Shouldn't happen — the conflict implies a row exists.
        raise RuntimeError(
            f"page ({issue_id}, seq={sequence}) vanished between insert and select"
        )
    return _coerce_uuid(row[0]), False


def page_has_chunks(cur: psycopg.Cursor, *, page_id: UUID, ocr_version: int) -> bool:
    """True iff at least one chunk row exists for (page_id, ocr_version)."""
    cur.execute(
        "select 1 from chunks where page_id = %s and ocr_version = %s limit 1",
        (page_id, ocr_version),
    )
    return cur.fetchone() is not None


# ---- chunks -----------------------------------------------------------

def insert_chunks(
    cur: psycopg.Cursor,
    *,
    page_id: UUID,
    ocr_version: int,
    rows: Sequence[ChunkRow],
) -> int:
    """Batch-insert chunks for one (page_id, ocr_version). Returns row count."""
    if not rows:
        return 0
    params: list[tuple[Any, ...]] = [
        (page_id, ocr_version, r.chunk_index, r.content, r.word_start, r.word_end, r.embedding)
        for r in rows
    ]
    cur.executemany(
        """
        insert into chunks
            (page_id, ocr_version, chunk_index, content, word_start, word_end, embedding)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (page_id, ocr_version, chunk_index) do nothing
        """,
        params,
    )
    return len(params)


# ---- utilities --------------------------------------------------------

def _fetch_one_id(cur: psycopg.Cursor) -> UUID:
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("insert/upsert did not return a row")
    return _coerce_uuid(row[0])


def _coerce_uuid(v: Any) -> UUID:
    if isinstance(v, UUID):
        return v
    return UUID(str(v))


def in_transaction(conn: psycopg.Connection):
    """Context manager that opens a transaction and rolls back on error.

    Sugar around ``conn.transaction()`` for sites that want to be explicit.
    """
    return conn.transaction()


def iter_paper_lccns(cur: psycopg.Cursor) -> Iterable[str]:
    cur.execute("select lccn from papers order by lccn")
    for (lccn,) in cur.fetchall():
        yield lccn


# ---- retrieval -------------------------------------------------------

@dataclass(frozen=True)
class ChunkHit:
    """A retrieval hit with full citation metadata.

    Populated by ``fetch_chunk_details`` after retrieval has narrowed
    down to the chunks worth showing. ``score`` is filled by the
    retrieval orchestrator (RRF, then rerank), not by SQL.
    """

    chunk_id: UUID
    content: str
    paper_lccn: str
    paper_title: str
    date_issued: date
    edition: int
    page_sequence: int
    image_url: str
    resource_url: str
    score: float = 0.0


# Common filter clause for retrieval queries. Joins to issues so callers
# can scope by paper or date window without each call having to repeat
# the boilerplate. Parameters are positional in the order the callers
# build them.
_FILTER_CLAUSE = """
  and (%(paper_id)s::uuid is null or i.paper_id = %(paper_id)s::uuid)
  and (%(date_from)s::date is null or i.date_issued >= %(date_from)s::date)
  and (%(date_to)s::date   is null or i.date_issued <= %(date_to)s::date)
"""


def semantic_search(
    cur: psycopg.Cursor,
    *,
    query_embedding: list[float],
    k: int,
    paper_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[tuple[UUID, float]]:
    """Top-k chunks by cosine distance to ``query_embedding``.

    Uses the HNSW partial index on ``chunks(embedding)`` where
    ``is_current = true``. Returns ``(chunk_id, distance)`` pairs in
    ascending distance order — smaller is more similar.
    """
    cur.execute(
        f"""
        select c.id, c.embedding <=> %(qvec)s::vector as distance
        from chunks c
        join pages  p on p.id = c.page_id
        join issues i on i.id = p.issue_id
        where c.is_current = true
        {_FILTER_CLAUSE}
        order by c.embedding <=> %(qvec)s::vector
        limit %(k)s
        """,
        {
            "qvec": query_embedding,
            "k": k,
            "paper_id": paper_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return [(_coerce_uuid(row[0]), float(row[1])) for row in cur.fetchall()]


def fts_search(
    cur: psycopg.Cursor,
    *,
    query: str,
    k: int,
    paper_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[tuple[UUID, float]]:
    """Top-k chunks by full-text relevance to ``query``.

    Uses ``websearch_to_tsquery`` (handles user-style queries with
    quoted phrases, OR, etc. without raising on syntax) and
    ``ts_rank_cd`` for ranking. Returns ``(chunk_id, rank)`` in
    descending rank order. Empty list when the tsquery is empty or
    no chunks match.
    """
    cur.execute(
        f"""
        select c.id, ts_rank_cd(c.fts, q) as rank
        from chunks c
        join pages  p on p.id = c.page_id
        join issues i on i.id = p.issue_id,
             websearch_to_tsquery('english', %(q)s) q
        where c.is_current = true
          and c.fts @@ q
        {_FILTER_CLAUSE}
        order by ts_rank_cd(c.fts, q) desc
        limit %(k)s
        """,
        {
            "q": query,
            "k": k,
            "paper_id": paper_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return [(_coerce_uuid(row[0]), float(row[1])) for row in cur.fetchall()]


def fetch_chunk_details(
    cur: psycopg.Cursor,
    *,
    chunk_ids: Sequence[UUID],
) -> dict[UUID, ChunkHit]:
    """Fetch citation metadata for a set of chunk ids.

    Returns a ``dict`` keyed by chunk_id so the caller can preserve
    its own ordering (e.g. the RRF + rerank order) when assembling the
    final list. Missing chunks are silently absent from the result.
    """
    if not chunk_ids:
        return {}
    cur.execute(
        """
        select
            c.id,
            c.content,
            pap.lccn,
            pap.title,
            i.date_issued,
            i.edition,
            p.sequence,
            p.image_url,
            c.page_id
        from chunks c
        join pages  p   on p.id = c.page_id
        join issues i   on i.id = p.issue_id
        join papers pap on pap.id = i.paper_id
        where c.id = any(%(ids)s::uuid[])
          and c.is_current = true
        """,
        {"ids": [str(cid) for cid in chunk_ids]},
    )
    out: dict[UUID, ChunkHit] = {}
    for row in cur.fetchall():
        chunk_id = _coerce_uuid(row[0])
        out[chunk_id] = ChunkHit(
            chunk_id=chunk_id,
            content=row[1],
            paper_lccn=row[2],
            paper_title=row[3],
            date_issued=row[4],
            edition=int(row[5]),
            page_sequence=int(row[6]),
            image_url=row[7],
            # Reconstruct resource_url from canonical pieces — we don't
            # store it on pages, but it's deterministic from LCCN+date+ed+seq.
            resource_url=_resource_url(row[2], row[4], int(row[5]), int(row[6])),
        )
    return out


def _resource_url(lccn: str, d: date, ed: int, seq: int) -> str:
    return (
        f"https://www.loc.gov/resource/{lccn}/{d.isoformat()}/ed-{ed}/seq-{seq}"
    )
