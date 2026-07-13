"""Unit tests for the SQL surface in herald.db.

We don't spin up Postgres here — the goal is to verify the SQL strings and
parameter shapes the writer produces. Integration is verified by running
the migration + ingest workflow against a real Supabase instance.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from herald.db import (
    ChunkRow,
    find_or_insert_page,
    insert_chunks,
    page_has_chunks,
    upsert_issue,
    upsert_paper,
)

PAPER_UUID = UUID("11111111-1111-1111-1111-111111111111")
ISSUE_UUID = UUID("22222222-2222-2222-2222-222222222222")
PAGE_UUID = UUID("33333333-3333-3333-3333-333333333333")


class FakeCursor:
    """Records execute() calls and lets tests pre-program fetch results."""

    def __init__(self, *, fetchone_returns: list | None = None) -> None:
        self.calls: list[tuple[str, tuple | None]] = []
        self._queue: list = list(fetchone_returns or [])
        self._executemany_calls: list[tuple[str, list[tuple]]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.calls.append((_norm(sql), tuple(params) if params else None))

    def executemany(self, sql: str, params_seq) -> None:
        self._executemany_calls.append((_norm(sql), list(params_seq)))

    def fetchone(self):
        return self._queue.pop(0) if self._queue else None


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def test_upsert_paper_emits_insert_with_excluded_update():
    cur = FakeCursor(fetchone_returns=[(PAPER_UUID,)])
    out = upsert_paper(
        cur,  # type: ignore[arg-type]
        lccn="sn83030213",
        title="New-York Daily Tribune",
        place="New York, N.Y.",
        start_year=1842,
        end_year=1866,
    )
    assert out == PAPER_UUID
    sql, params = cur.calls[0]
    assert "insert into papers" in sql
    assert "on conflict (lccn) do update" in sql
    assert "returning id" in sql
    assert params == ("sn83030213", "New-York Daily Tribune", "New York, N.Y.", 1842, 1866)


def test_upsert_paper_raises_if_no_row_returned():
    cur = FakeCursor()
    with pytest.raises(RuntimeError, match="did not return"):
        upsert_paper(cur, lccn="x", title="x")  # type: ignore[arg-type]


def test_upsert_issue_emits_correct_sql_and_params():
    cur = FakeCursor(fetchone_returns=[(ISSUE_UUID,)])
    out = upsert_issue(
        cur,  # type: ignore[arg-type]
        paper_id=PAPER_UUID,
        date_issued=date(1845, 8, 9),
        edition=1,
        loc_url="https://chroniclingamerica.loc.gov/lccn/sn83030213/1845-08-09/ed-1.json",
    )
    assert out == ISSUE_UUID
    sql, params = cur.calls[0]
    assert "insert into issues" in sql
    assert "on conflict (paper_id, date_issued, edition) do update" in sql
    assert params is not None
    assert params[:3] == (PAPER_UUID, date(1845, 8, 9), 1)


def test_find_or_insert_page_when_new_returns_was_new_true():
    cur = FakeCursor(fetchone_returns=[(PAGE_UUID,)])
    page_id, was_new = find_or_insert_page(
        cur,  # type: ignore[arg-type]
        issue_id=ISSUE_UUID, sequence=2,
        image_url="i.jpg", jp2_url="i.jp2", pdf_url="i.pdf",
        ocr_text="ANTI-RENT EXCITEMENT",
    )
    assert page_id == PAGE_UUID
    assert was_new is True
    # Only the INSERT was issued (no second SELECT)
    assert len(cur.calls) == 1
    assert "insert into pages" in cur.calls[0][0]
    assert "on conflict (issue_id, sequence) do nothing" in cur.calls[0][0]


def test_find_or_insert_page_when_existing_returns_was_new_false():
    # Insert returns no row (conflict), then SELECT finds it
    cur = FakeCursor(fetchone_returns=[None, (PAGE_UUID,)])
    page_id, was_new = find_or_insert_page(
        cur,  # type: ignore[arg-type]
        issue_id=ISSUE_UUID, sequence=2,
        image_url="i.jpg", jp2_url=None, pdf_url=None, ocr_text=None,
    )
    assert page_id == PAGE_UUID
    assert was_new is False
    assert len(cur.calls) == 2
    assert "insert into pages" in cur.calls[0][0]
    assert "select id from pages where issue_id = %s and sequence = %s" in cur.calls[1][0]


def test_find_or_insert_page_raises_if_conflict_then_select_empty():
    cur = FakeCursor(fetchone_returns=[None, None])
    with pytest.raises(RuntimeError, match="vanished"):
        find_or_insert_page(
            cur,  # type: ignore[arg-type]
            issue_id=ISSUE_UUID, sequence=2,
            image_url="i.jpg", jp2_url=None, pdf_url=None, ocr_text=None,
        )


def test_page_has_chunks_true_when_row_exists():
    cur = FakeCursor(fetchone_returns=[(1,)])
    assert page_has_chunks(cur, page_id=PAGE_UUID, ocr_version=1) is True  # type: ignore[arg-type]
    sql, params = cur.calls[0]
    assert "select 1 from chunks" in sql
    assert "where page_id = %s and ocr_version = %s" in sql
    assert "limit 1" in sql
    assert params == (PAGE_UUID, 1)


def test_page_has_chunks_false_when_empty():
    cur = FakeCursor()
    assert page_has_chunks(cur, page_id=PAGE_UUID, ocr_version=1) is False  # type: ignore[arg-type]


def test_insert_chunks_uses_executemany_with_correct_params():
    cur = FakeCursor()
    rows = [
        ChunkRow(chunk_index=0, content="hello", word_start=0, word_end=1, embedding=[0.1]*4),
        ChunkRow(chunk_index=1, content="world", word_start=1, word_end=2, embedding=[0.2]*4),
    ]
    n = insert_chunks(cur, page_id=PAGE_UUID, ocr_version=1, rows=rows)  # type: ignore[arg-type]
    assert n == 2
    sql, params_list = cur._executemany_calls[0]
    assert "insert into chunks" in sql
    assert "on conflict (page_id, ocr_version, chunk_index) do nothing" in sql
    assert len(params_list) == 2
    assert params_list[0] == (PAGE_UUID, 1, 0, "hello", 0, 1, [0.1]*4)
    assert params_list[1] == (PAGE_UUID, 1, 1, "world", 1, 2, [0.2]*4)


def test_insert_chunks_empty_is_noop():
    cur = FakeCursor()
    n = insert_chunks(cur, page_id=PAGE_UUID, ocr_version=1, rows=[])  # type: ignore[arg-type]
    assert n == 0
    assert cur._executemany_calls == []
