"""Ingest orchestrator: LOC → normalize → chunk → embed → write.

The pipeline is async (LOC + Voyage are HTTP-bound) with synchronous
psycopg writes wrapped in transactions per page. Embeddings are batched
across pages so we make ~1k Voyage calls instead of ~13k.

Idempotency rules:
- ``papers`` / ``issues`` are upserted (ON CONFLICT DO UPDATE).
- A page is considered "fully processed" if its row exists and
  ``ocr_text`` is not null. On resume, processed pages are skipped without
  fetching OCR.
- Page row creation, ``ocr_text`` update, and chunk inserts all run in a
  single transaction per page, so a crash mid-flight leaves the DB clean.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import psycopg

from herald import db
from herald.chunker import ChunkSpan, chunk_text
from herald.embed import VoyageEmbedder
from herald.loc import LOCClient, PageRef
from herald.normalize import normalize_ocr

OnPage = Callable[[PageRef, str], None]

logger = logging.getLogger(__name__)

DEFAULT_EMBED_BATCH = 128  # max inputs per Voyage request


@dataclass(frozen=True)
class IngestStats:
    issues_seen: int = 0
    pages_seen: int = 0
    pages_skipped: int = 0
    pages_written: int = 0
    chunks_written: int = 0


@dataclass
class _PageWork:
    issue_id: UUID
    page_ref: PageRef
    text: str
    spans: list[ChunkSpan]


async def ingest_paper(
    *,
    loc: LOCClient,
    voyage: VoyageEmbedder,
    conn: psycopg.Connection,
    lccn: str,
    title: str,
    place: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    embed_batch_chunks: int = DEFAULT_EMBED_BATCH,
    on_page: OnPage | None = None,
) -> IngestStats:
    """Ingest one paper end-to-end.

    Returns aggregate stats. The ``on_page`` callback (if given) is invoked
    with ``(page_ref, status)`` for each page, where status is one of
    ``"skipped"`` / ``"written"`` / ``"empty"`` — useful for progress bars.
    """
    # 1. Upsert paper row in its own tiny transaction.
    with conn.cursor() as cur, conn.transaction():
        paper_id = db.upsert_paper(
            cur, lccn=lccn, title=title, place=place,
            start_year=start_year, end_year=end_year,
        )

    stats = IngestStats()
    buffer: list[_PageWork] = []
    buffer_chunk_count = 0

    async for issue, pages in loc.iter_issues_with_pages(
        lccn, date_from=date_from, date_to=date_to,
    ):
        stats = _bump(stats, issues_seen=1)
        with conn.cursor() as cur, conn.transaction():
            issue_id = db.upsert_issue(
                cur, paper_id=paper_id, date_issued=issue.date_issued,
                edition=issue.edition, loc_url=issue.url,
            )

        for page_ref in pages:
            stats = _bump(stats, pages_seen=1)
            if _page_already_processed(conn, issue_id=issue_id, sequence=page_ref.sequence):
                stats = _bump(stats, pages_skipped=1)
                if on_page is not None:
                    on_page(page_ref, "skipped")
                continue

            raw = await loc.fetch_ocr(page_ref)
            text = normalize_ocr(raw)
            spans = chunk_text(text) if text else []
            buffer.append(_PageWork(
                issue_id=issue_id, page_ref=page_ref, text=text, spans=spans,
            ))
            buffer_chunk_count += len(spans)

            if buffer_chunk_count >= embed_batch_chunks:
                stats = await _flush(
                    voyage=voyage, conn=conn, buffer=buffer, stats=stats, on_page=on_page,
                )
                buffer = []
                buffer_chunk_count = 0

    if buffer:
        stats = await _flush(
            voyage=voyage, conn=conn, buffer=buffer, stats=stats, on_page=on_page,
        )
    return stats


def _page_already_processed(
    conn: psycopg.Connection, *, issue_id: UUID, sequence: int
) -> bool:
    """A page is "processed" iff at least one current chunk row exists.

    Previously the check was ``pages.ocr_text IS NOT NULL``, but an
    empty-OCR write (e.g. when fetch_ocr returned "") still leaves
    ``ocr_text = ''`` which trips that predicate. The chunk-based check
    is the right one: if we never got chunks, we never finished the
    page's real work and should re-attempt it.

    Pages that legitimately have no OCR (image-only) will be re-probed
    on every ingest run. Cheap (2 HTTP requests per retry) and rare for
    the corpora we care about.
    """
    with conn.cursor() as cur, conn.transaction():
        cur.execute(
            """
            select exists (
                select 1
                from chunks c
                join pages  p on p.id = c.page_id
                where p.issue_id = %s
                  and p.sequence = %s
                  and c.is_current = true
            )
            """,
            (issue_id, sequence),
        )
        row = cur.fetchone()
    return bool(row and row[0])


async def _flush(
    *,
    voyage: VoyageEmbedder,
    conn: psycopg.Connection,
    buffer: list[_PageWork],
    stats: IngestStats,
    on_page: OnPage | None,
) -> IngestStats:
    # Collapse all spans across the buffered pages into one Voyage call.
    all_texts: list[str] = []
    boundaries: list[int] = []
    for wu in buffer:
        boundaries.append(len(all_texts))
        all_texts.extend(s.content for s in wu.spans)
    boundaries.append(len(all_texts))

    if all_texts:
        embeddings = await voyage.embed_documents(all_texts)
    else:
        embeddings = []

    for i, wu in enumerate(buffer):
        start, end = boundaries[i], boundaries[i + 1]
        wu_embeds = embeddings[start:end]
        # One transaction per page: page row + ocr_text + chunks atomically.
        with conn.cursor() as cur, conn.transaction():
            page_id, _was_new = db.find_or_insert_page(
                cur,
                issue_id=wu.issue_id,
                sequence=wu.page_ref.sequence,
                image_url=wu.page_ref.image_url,
                jp2_url=wu.page_ref.jp2_url,
                pdf_url=wu.page_ref.pdf_url,
                ocr_text=wu.text,
            )
            # If the page already existed (from a prior partial run) without
            # ocr_text, fill it now. Idempotent: no-op when already set.
            cur.execute(
                "update pages set ocr_text = %s where id = %s and ocr_text is null",
                (wu.text, page_id),
            )
            chunk_rows = [
                db.ChunkRow(
                    chunk_index=s.index,
                    content=s.content,
                    word_start=s.word_start,
                    word_end=s.word_end,
                    embedding=e,
                )
                for s, e in zip(wu.spans, wu_embeds, strict=True)
            ]
            written = db.insert_chunks(
                cur, page_id=page_id, ocr_version=1, rows=chunk_rows,
            )
        if not wu.spans:
            stats = _bump(stats, pages_written=1)  # row written, just empty OCR
            if on_page is not None:
                on_page(wu.page_ref, "empty")
        else:
            stats = _bump(stats, pages_written=1, chunks_written=written)
            if on_page is not None:
                on_page(wu.page_ref, "written")
    return stats


def _bump(stats: IngestStats, **deltas: int) -> IngestStats:
    return IngestStats(
        issues_seen=stats.issues_seen + deltas.get("issues_seen", 0),
        pages_seen=stats.pages_seen + deltas.get("pages_seen", 0),
        pages_skipped=stats.pages_skipped + deltas.get("pages_skipped", 0),
        pages_written=stats.pages_written + deltas.get("pages_written", 0),
        chunks_written=stats.chunks_written + deltas.get("chunks_written", 0),
    )
