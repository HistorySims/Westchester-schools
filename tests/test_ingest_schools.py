"""Tests for the schools ingest adapter (manifest → text → chunks → write)."""

from __future__ import annotations

import asyncio
import datetime as _dt
from pathlib import Path
from uuid import UUID

import fitz
import pytest

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
from herald.pdf_text import ExtractedDoc, TableBlock, extract_pdf, extract_pdf_text, sanitize
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


def test_extract_pdf_prose_only(tmp_path):
    # A document with no detectable grid extracts exactly like plain text:
    # all prose, no tables, and content_chars == the prose length.
    pdf = tmp_path / "agenda.pdf"
    _make_pdf(pdf, AGENDA_TEXT)
    doc = extract_pdf(pdf)
    assert doc.page_count == 1
    assert doc.tables == []
    assert "Jane Smith" in doc.text
    assert doc.content_chars == len(doc.text)


def test_sanitize_strips_nul():
    # PyMuPDF occasionally emits NUL bytes; Postgres text columns reject them.
    assert sanitize("Board\x00 of\x00 Ed") == "Board of Ed"
    assert "\x00" not in sanitize("a\x00b")


# ---- chunk preparation -------------------------------------------------

def test_prepare_document_dates_types_and_outline():
    entry = _entry("x.pdf", doc_type=DocType.other, title="BOE Regular Meeting 3-17")
    chunks, meeting_date, doc_type = prepare_document(entry, AGENDA_TEXT)
    assert meeting_date == _dt.date(2026, 3, 17)   # parsed from content
    assert doc_type == "agenda"                     # refined from title
    paths = [c.section_path for c in chunks]
    assert any(p.startswith("P2") for p in paths)   # outline captured
    assert all(c.district == "peekskill" for c in chunks)


def test_prepare_document_appends_whole_table_chunks():
    # Detected tables ride along as single kind='table' chunks: kept whole (no
    # MIN_CHUNK_CHARS floor, no splitting), with order_index continuing past the
    # prose so chunk_index stays unique, and doc metadata stamped like any chunk.
    entry = _entry("x.pdf")
    tables = [
        TableBlock(page=4, markdown="| lane | step | salary |\n|---|---|---|\n| MA | 5 | 65,000 |"),
        TableBlock(page=4, markdown="| tier | stipend |\n|---|---|\n| Head Coach | 8,500 |"),
    ]
    chunks, _, _ = prepare_document(entry, AGENDA_TEXT, tables)
    prose = [c for c in chunks if c.kind == "prose"]
    tbl = [c for c in chunks if c.kind == "table"]
    assert len(tbl) == 2
    assert prose, "prose chunks should still be present"
    # tables come after every prose chunk_index and don't collide
    idxs = [c.order_index for c in chunks]
    assert len(idxs) == len(set(idxs))
    assert min(c.order_index for c in tbl) > max(c.order_index for c in prose)
    # whole grid preserved verbatim; section metadata marks it as a table
    assert tbl[0].content.startswith("| lane | step | salary |")
    assert all(c.section_type == "Table" for c in tbl)
    assert {c.section_path for c in tbl} == {"T4#1", "T4#2"}
    assert all(c.district == "peekskill" for c in tbl)


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
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._conn.calls.append((" ".join(sql.split()), params))
        sql_l = sql.lower()
        if "insert into districts" in sql_l:
            self._conn._fetch = (DISTRICT_UUID,)
        elif "insert into documents" in sql_l:
            self._conn._fetch = (DOC_UUID, self._conn.existing_status)
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
        self.existing_status = "pending"   # what find_or_insert_document reports

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


def test_tables_backfill_adds_only_table_chunks(tmp_path, monkeypatch):
    # A corpus ingested before table-aware chunking: the backfill reprocesses
    # an already-ingested doc and inserts ONLY its table chunk — prose and the
    # document row are left alone.
    from herald import ingest_schools

    raw = tmp_path / "data" / "raw"
    pdf = raw / "peekskill" / "agenda" / "ab.pdf"
    pdf.parent.mkdir(parents=True)
    _make_pdf(pdf, AGENDA_TEXT)

    def fake_extract(_path):
        return ExtractedDoc(
            text=AGENDA_TEXT,
            tables=[TableBlock(page=2, markdown="| lane | step |\n|---|---|\n| MA | 5 |")],
            page_count=2,
        )
    monkeypatch.setattr(ingest_schools, "extract_pdf", fake_extract)

    class IngestedCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "insert into documents" in sql.lower():
                self._conn._fetch = None                     # conflict
            elif "select id, ingest_status" in sql.lower():
                self._conn._fetch = (DOC_UUID, "ingested")   # already ingested

    class IngestedConn(FakeConn):
        def cursor(self):
            return IngestedCursor(self)

    conn, voyage = IngestedConn(), FakeVoyage()
    stats = asyncio.run(ingest_manifests(
        [(_entry(str(pdf)), raw / "manifest.jsonl")],
        conn=conn, voyage=voyage, tables_only=True,
    ))
    assert stats.docs_tables_backfilled == 1
    assert stats.docs_skipped == 0
    # only the whole-table chunk was embedded + inserted, not the prose
    assert stats.chunks_written == 1
    assert len(voyage.texts) == 1
    _, rows = conn.many[0]
    assert len(rows) == 1
    assert rows[0][-1] == "table"     # kind column
    # backfill leaves the already-ingested document row untouched
    assert [p for sql, p in conn.calls if "update documents set" in sql] == []


def test_tables_backfill_skips_not_yet_ingested(tmp_path, monkeypatch):
    # A doc not yet in the corpus is left for a normal ingest pass, not backfilled.
    from herald import ingest_schools

    raw = tmp_path / "data" / "raw"
    pdf = raw / "peekskill" / "agenda" / "ab.pdf"
    pdf.parent.mkdir(parents=True)
    _make_pdf(pdf, AGENDA_TEXT)

    def fake_extract(_path):
        return ExtractedDoc(text=AGENDA_TEXT, tables=[], page_count=1)
    monkeypatch.setattr(ingest_schools, "extract_pdf", fake_extract)

    conn = FakeConn()   # find_or_insert returns (DOC_UUID, "pending")
    stats = asyncio.run(ingest_manifests(
        [(_entry(str(pdf)), raw / "manifest.jsonl")],
        conn=conn, voyage=FakeVoyage(), tables_only=True,
    ))
    assert stats.docs_tables_backfilled == 0
    assert stats.docs_skipped == 1        # not-ingested → left alone
    assert conn.many == []                # nothing inserted


def test_no_text_document_records_its_page_count(tmp_path):
    # A scanned PDF is marked no_text — and no_text is exactly the set we may
    # later pay a per-page vision bill to OCR. Marking the status without the
    # page count left the entire OCR backlog costed at NULL: the database knew
    # which documents needed OCR but not how big any of them was.
    raw = tmp_path / "data" / "raw"
    pdf = raw / "port-chester-rye" / "minutes" / "scanned.pdf"
    pdf.parent.mkdir(parents=True)
    doc = fitz.open()
    for _ in range(3):
        doc.new_page()        # image-less, text-less: extracts as nothing
    doc.save(str(pdf))
    doc.close()

    conn = FakeConn()
    stats = asyncio.run(ingest_manifests(
        [(_entry(str(pdf), district="port-chester-rye"), raw / "manifest.jsonl")],
        conn=conn, voyage=FakeVoyage(),
    ))
    assert stats.docs_no_text == 1 and stats.chunks_written == 0

    marks = [p for sql, p in conn.calls if "update documents set" in sql]
    assert marks, "the document was never marked"
    status, _err, _date, _type, page_count, text_chars = marks[0][:6]
    assert status == "no_text"
    assert page_count == 3, "page count dropped — OCR cost is unknowable without it"
    assert text_chars == 0


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


# ---- tables-db backfill (re-fetch from source_url) -----------------------

class _FakeResp:
    def __init__(self, content: bytes, ctype: str = "application/pdf"):
        self.content = content
        self.headers = {"content-type": ctype}


class _FakeFetcher:
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    def get(self, url: str, **kwargs) -> _FakeResp:
        return self._resp


def test_candidate_docs_sql_variants():
    from herald.ingest_schools import _candidate_docs_sql

    base = _candidate_docs_sql(district=False, only_missing=False, limit=False)
    assert "d.ingest_status = 'ingested'" in base
    assert "join districts di" in base
    assert "%(district)s" not in base
    assert "limit" not in base.lower()

    full = _candidate_docs_sql(district=True, only_missing=True, limit=True)
    assert "di.slug = %(district)s" in full
    assert "d.tables_extracted_at is null" in full
    assert "not exists" in full and "c.kind = 'table'" in full
    assert full.strip().endswith("limit %(limit)s")


def test_boarddocs_public_url():
    from herald.ingest_schools import _boarddocs_public_url

    u = "https://go.boarddocs.com/ny/elmsford/Board.nsf/files/ABC123/$file/x.pdf"
    assert _boarddocs_public_url(u) == (
        "https://go.boarddocs.com/ny/elmsford/Board.nsf/Public"
    )
    assert _boarddocs_public_url("https://www.eufsd.org/handbook.pdf") is None


def test_fetch_pdf_tables_rejects_non_pdf():
    from herald.ingest_schools import _fetch_pdf_tables

    f = _FakeFetcher(_FakeResp(b"<html>not a pdf</html>", ctype="text/html"))
    with pytest.raises(ValueError):
        _fetch_pdf_tables(f, "https://example.org/x", primed=set())


def test_fetch_pdf_tables_reads_prose_pdf(tmp_path):
    # A born-digital prose PDF fetched from its URL parses cleanly to zero tables
    # (no error) — the accept path, even when a served content-type is generic.
    from herald.ingest_schools import _fetch_pdf_tables

    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, AGENDA_TEXT)
    f = _FakeFetcher(_FakeResp(pdf.read_bytes(), ctype="application/octet-stream"))
    assert _fetch_pdf_tables(f, "https://example.org/a.pdf", primed=set()) == []


class _FakeBrowser:
    def __init__(self, data: bytes):
        self._data = data
        self.primed: list[str] = []

    async def prime(self, url: str) -> None:
        self.primed.append(url)

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        return self._data


def test_tables_from_bytes_prose_pdf(tmp_path):
    from herald.ingest_schools import _tables_from_bytes

    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, AGENDA_TEXT)
    assert _tables_from_bytes(pdf.read_bytes()) == []


def test_browser_pdf_tables_primes_and_extracts(tmp_path):
    from herald.ingest_schools import _browser_pdf_tables

    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, AGENDA_TEXT)
    b = _FakeBrowser(pdf.read_bytes())
    bd = "https://go.boarddocs.com/ny/elmsford/Board.nsf/files/A/$file/x.pdf"
    tables = asyncio.run(_browser_pdf_tables(b, bd))
    assert tables == []   # prose PDF: no tables, no error
    assert b.primed == ["https://go.boarddocs.com/ny/elmsford/Board.nsf/Public"]


def test_browser_pdf_tables_rejects_non_pdf():
    from herald.ingest_schools import _browser_pdf_tables

    b = _FakeBrowser(b"<html>not a pdf</html>")
    bd = "https://go.boarddocs.com/ny/x/Board.nsf/files/A/$file/x.pdf"
    with pytest.raises(ValueError):
        asyncio.run(_browser_pdf_tables(b, bd))


def test_fetch_pdf_tables_primes_boarddocs_session(tmp_path):
    # BoardDocs URLs prime the /Public page once (session cookie) and send a
    # Referer; a plain host is fetched directly with neither.
    from herald.ingest_schools import _fetch_pdf_tables

    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, AGENDA_TEXT)

    class RecordingFetcher:
        def __init__(self, body: bytes):
            self._body = body
            self.gets: list[tuple[str, dict]] = []

        def get(self, url, **kw):
            self.gets.append((url, kw.get("headers", {})))
            return _FakeResp(self._body)

    f = RecordingFetcher(pdf.read_bytes())
    primed: set[str] = set()
    bd = "https://go.boarddocs.com/ny/elmsford/Board.nsf/files/A/$file/x.pdf"
    _fetch_pdf_tables(f, bd, primed=primed)
    # first GET is the /Public prime, second is the file with a Referer
    assert f.gets[0][0] == "https://go.boarddocs.com/ny/elmsford/Board.nsf/Public"
    assert f.gets[1][0] == bd
    assert f.gets[1][1].get("Referer", "").endswith("/Board.nsf/Public")
    # a second BoardDocs file for the same district doesn't re-prime
    f.gets.clear()
    _fetch_pdf_tables(f, bd, primed=primed)
    assert len(f.gets) == 1 and f.gets[0][0] == bd


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
        SchoolChunkRow(chunk_index=1, section_path="T4#1", section_type="Table",
                       heading="Table (p. 4)", content="| a | b |", embedding=None,
                       meeting_date=None, doc_type="agenda", kind="table"),
    ])
    assert n == 2
    sql, rows = conn.many[0]
    assert "on conflict (document_id, chunk_index) do nothing" in sql
    assert "kind" in sql
    assert rows[0][0] == doc_id
    assert rows[0][-1] == "prose"   # default kind
    assert rows[1][-1] == "table"   # explicit table chunk
