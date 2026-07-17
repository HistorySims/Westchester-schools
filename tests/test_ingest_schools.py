"""Tests for the schools ingest adapter (manifest → text → chunks → write)."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path
from uuid import UUID

import fitz

from herald.chunking import Chunk
from herald.ingest_schools import (
    embed_input,
    find_manifests,
    ingest_manifests,
    load_manifest,
    prepare_document,
    render_report,
    resolve_local_path,
)
from herald.pdf_text import extract_pdf_text
from herald.schools_db import (
    SchoolChunkRow,
    find_or_insert_document,
    insert_chunks,
    upsert_district,
)
from herald.scrape.models import DocType, ManifestEntry

DISTRICT_UUID = UUID("11111111-1111-1111-1111-111111111111")
DOC_UUID = UUID("22222222-2222-2222-2222-222222222222")

AGENDA_TEXT = """Board of Education Regular Meeting
March 17, 2026

1. Call to Order
The meeting was called to order at 7:00 PM by the Board President.

2. Consent Agenda - Personnel
A. Appointment of Jane Smith as probationary teacher of mathematics effective
September 1, 2026, at Step 3 of the salary schedule pending certification.
B. Resignation of John Doe, custodial staff, effective June 30, 2026, is
accepted with thanks for eleven years of service to the district schools.
"""


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(36, 36, 560, 800), text)
    doc.save(str(path))
    doc.close()


def _entry(local_path: str, **kw) -> ManifestEntry:
    defaults = dict(
        district="peekskill",
        doc_type=DocType.agenda,
        title="Regular Meeting Agenda",
        source_url="https://go.boarddocs.com/ny/pcsd/files/X/$file/agenda.pdf",
        local_path=local_path,
        sha256="a" * 64,
        size_bytes=1234,
        content_type="application/pdf",
        fetched_at=_dt.datetime(2026, 7, 1, tzinfo=_dt.UTC),
    )
    defaults.update(kw)
    return ManifestEntry(**defaults)


# ---- pdf extraction ----------------------------------------------------

def test_extract_pdf_text(tmp_path):
    pdf = tmp_path / "agenda.pdf"
    _make_pdf(pdf, AGENDA_TEXT)
    got = extract_pdf_text(pdf)
    assert got.page_count == 1
    assert "Call to Order" in got.text
    assert "Jane Smith" in got.text


# ---- chunk preparation -------------------------------------------------

def test_prepare_document_dates_types_and_outline():
    entry = _entry("x.pdf", doc_type=DocType.other, title="BOE Regular Meeting 3-17")
    chunks, meeting_date, doc_type = prepare_document(entry, AGENDA_TEXT)
    assert meeting_date == _dt.date(2026, 3, 17)   # parsed from content
    assert doc_type == "agenda"                     # refined from title
    paths = [c.section_path for c in chunks]
    assert any(p.startswith("P2") for p in paths)   # outline captured
    assert all(c.district == "peekskill" for c in chunks)


def test_prepare_document_date_priority():
    # Scrape-time dates can be placeholders (BoardDocs stamps the school-year
    # end on every file) — title, then document header, outrank the manifest.
    entry = _entry("x.pdf", title="Business Meeting - June 2 2026.pdf",
                   date=_dt.date(2026, 12, 31))
    _, meeting_date, _ = prepare_document(entry, AGENDA_TEXT)
    assert meeting_date == _dt.date(2026, 6, 2)          # from the title

    entry = _entry("x.pdf", date=_dt.date(2026, 12, 31))  # dateless title
    _, meeting_date, _ = prepare_document(entry, AGENDA_TEXT)
    assert meeting_date == _dt.date(2026, 3, 17)          # from the header

    entry = _entry("x.pdf", date=_dt.date(2026, 1, 5))
    _, meeting_date, _ = prepare_document(entry, "No dates anywhere " * 20)
    assert meeting_date == _dt.date(2026, 1, 5)           # manifest fallback


def test_embed_input_breadcrumb():
    c = Chunk(
        content="Approval of the BOCES cooperative bid.",
        section_path="P2.A", section_type="Consent Agenda - Business",
        heading="BOCES Bid", order_index=3,
        district="peekskill", meeting_date=_dt.date(2026, 3, 17), doc_type="agenda",
    )
    s = embed_input(c)
    assert s.startswith(
        "peekskill · 2026-03-17 · Consent Agenda - Business \u203a BOCES Bid"
    )
    assert s.endswith("Approval of the BOCES cooperative bid.")


# ---- manifest handling ---------------------------------------------------

def test_resolve_local_path_falls_back_to_manifest_dir(tmp_path):
    # Recorded on the scrape runner as data/raw/...; here the artifact was
    # downloaded elsewhere, so only the tail relative to the manifest holds.
    raw = tmp_path / "artifacts" / "site-peekskill" / "data" / "raw"
    f = raw / "peekskill" / "agenda" / "aaaa_agenda.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF")
    entry = _entry("data/raw/peekskill/agenda/aaaa_agenda.pdf")
    assert resolve_local_path(entry, raw / "manifest.jsonl") == f
    missing = _entry("data/raw/peekskill/agenda/other.pdf")
    assert resolve_local_path(missing, raw / "manifest.jsonl") is None


def test_find_and_load_manifests(tmp_path):
    m = tmp_path / "a" / "data" / "raw" / "manifest.jsonl"
    m.parent.mkdir(parents=True)
    m.write_text(_entry("x.pdf").model_dump_json() + "\n\n", encoding="utf-8")
    found = find_manifests(tmp_path)
    assert found == [m]
    entries = load_manifest(m)
    assert len(entries) == 1 and entries[0].district == "peekskill"


# ---- dry-run pipeline ----------------------------------------------------

def test_ingest_dry_run_end_to_end(tmp_path):
    raw = tmp_path / "data" / "raw"
    pdf = raw / "peekskill" / "agenda" / "ab_agenda.pdf"
    pdf.parent.mkdir(parents=True)
    _make_pdf(pdf, AGENDA_TEXT)
    mpath = raw / "manifest.jsonl"
    entry = _entry(str(pdf))
    scanned = _entry(str(raw / "peekskill" / "agenda" / "gone.pdf"), sha256="b" * 64)

    stats = asyncio.run(ingest_manifests([(entry, mpath), (scanned, mpath)]))
    assert stats.docs_seen == 2
    assert stats.docs_ingested == 1
    assert stats.docs_missing == 1
    assert stats.chunks_written >= 2
    assert stats.by_district["peekskill"] == stats.chunks_written
    report = render_report(stats, dry_run=True)
    assert "DRY RUN" in report and "peekskill" in report


# ---- real-run pipeline against fakes -------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        sql_l = sql.lower()
        if "insert into districts" in sql_l:
            self._conn._fetch = (DISTRICT_UUID,)
        elif "insert into documents" in sql_l:
            self._conn._fetch = (DOC_UUID, "pending")
        else:
            self._conn._fetch = None

    def executemany(self, sql, seq):
        self._conn.many.append((" ".join(sql.split()), list(seq)))

    def fetchone(self):
        return self._conn._fetch


class FakeConn:
    """Just enough of psycopg.Connection for the ingest orchestrator."""

    def __init__(self):
        self.calls: list = []
        self.many: list = []
        self._fetch = None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext()


class FakeVoyage:
    def __init__(self):
        self.texts: list[str] = []

    async def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[0.0] * 4 for _ in texts]


def test_ingest_real_run_writes_chunks_and_marks_document(tmp_path):
    raw = tmp_path / "data" / "raw"
    pdf = raw / "peekskill" / "agenda" / "ab_agenda.pdf"
    pdf.parent.mkdir(parents=True)
    _make_pdf(pdf, AGENDA_TEXT)
    conn, voyage = FakeConn(), FakeVoyage()

    stats = asyncio.run(
        ingest_manifests([(_entry(str(pdf)), raw / "manifest.jsonl")],
                         conn=conn, voyage=voyage)
    )
    assert stats.docs_ingested == 1 and stats.chunks_written > 0
    # every chunk got a contextual-prefix embedding input
    assert len(voyage.texts) == stats.chunks_written
    assert all(t.startswith("peekskill ·") for t in voyage.texts)
    # chunks batch-inserted; document marked ingested
    assert any("insert into chunks" in sql for sql, _ in conn.many)
    marks = [p for sql, p in conn.calls if "update documents set" in sql]
    assert marks and marks[0][0] == "ingested"


def test_ingest_skips_already_ingested(tmp_path):
    class DoneCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "insert into documents" in sql.lower():
                self._conn._fetch = None  # conflict: no returning row
            elif "select id, ingest_status" in sql.lower():
                self._conn._fetch = (DOC_UUID, "ingested")

    class DoneConn(FakeConn):
        def cursor(self):
            return DoneCursor(self)

    stats = asyncio.run(
        ingest_manifests([(_entry("never-touched.pdf"), tmp_path / "manifest.jsonl")],
                         conn=DoneConn())
    )
    assert stats.docs_skipped == 1 and stats.docs_ingested == 0


# ---- SQL shapes ----------------------------------------------------------

def test_schools_db_sql_shapes():
    conn = FakeConn()
    cur = conn.cursor()
    did = upsert_district(cur, slug="peekskill")
    assert did == DISTRICT_UUID
    doc_id, status = find_or_insert_document(
        cur, district_id=did, doc_type="agenda", title="t",
        source_url="u", sha256="a" * 64,
    )
    assert (doc_id, status) == (DOC_UUID, "pending")
    n = insert_chunks(cur, document_id=doc_id, district_id=did, rows=[
        SchoolChunkRow(chunk_index=0, section_path="P1", section_type="Call to Order",
                       heading="Call to Order", content="x" * 50, embedding=None,
                       meeting_date=None, doc_type="agenda"),
    ])
    assert n == 1
    sql, rows = conn.many[0]
    assert "on conflict (document_id, chunk_index) do nothing" in sql
    assert rows[0][0] == doc_id
