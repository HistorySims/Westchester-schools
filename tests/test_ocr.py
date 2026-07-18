"""Tests for the OCR fallback and OCR-mode ingest.

Tesseract's binary isn't available in unit tests, so ``pytesseract`` is
monkeypatched — we verify the rasterize→OCR→chunk→write *wiring*, not
Tesseract itself (its quality is validated by a real Actions run).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import types
from pathlib import Path

import fitz

from herald import ocr as ocr_mod
from herald.ingest_schools import ingest_manifests, render_report
from herald.scrape.models import DocType, ManifestEntry


def _text_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(36, 36, 560, 800), text)
    doc.save(str(path))
    doc.close()


def _blank_pdf(path: Path) -> None:
    # A page with no text layer — stands in for a scanned/image PDF.
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def _entry(local_path: str, **kw) -> ManifestEntry:
    defaults = dict(
        district="port-chester-rye",
        doc_type=DocType.agenda,
        title="March 18, 2021 Agenda",
        source_url="https://portchesterschools.org/a.pdf",
        local_path=local_path,
        sha256="a" * 64,
        size_bytes=1,
        fetched_at=_dt.datetime(2026, 7, 1, tzinfo=_dt.UTC),
    )
    defaults.update(kw)
    return ManifestEntry(**defaults)


def _fake_pytesseract(text: str) -> types.ModuleType:
    mod = types.ModuleType("pytesseract")
    mod.image_to_string = lambda img: text  # type: ignore[attr-defined]
    return mod


def test_ocr_pdf_rasterizes_and_calls_tesseract(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", _fake_pytesseract("RECOVERED TEXT"))
    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)  # no text layer
    got = ocr_mod.ocr_pdf(pdf)
    assert got.page_count == 1
    assert "RECOVERED TEXT" in got.text


def test_ocr_pdf_respects_max_pages(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting_image_to_string(img):
        calls["n"] += 1
        return "x"

    mod = types.ModuleType("pytesseract")
    mod.image_to_string = counting_image_to_string  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", mod)

    doc = fitz.open()
    for _ in range(3):
        doc.new_page()
    pdf = tmp_path / "multi.pdf"
    doc.save(str(pdf))
    doc.close()

    got = ocr_mod.ocr_pdf(pdf, max_pages=2)
    assert calls["n"] == 2          # only two pages OCR'd
    assert got.page_count == 3      # but true page count preserved


def test_ocr_mode_dry_run_counts_candidates_only(tmp_path):
    raw = tmp_path / "data" / "raw"
    (raw / "port-chester-rye" / "agenda").mkdir(parents=True)
    scan = raw / "port-chester-rye" / "agenda" / "aa_scan.pdf"
    _blank_pdf(scan)                       # no-text -> candidate
    born = raw / "port-chester-rye" / "agenda" / "bb_born.pdf"
    _text_pdf(born, "1. Call to Order\nThe board met. " * 20)  # has text -> skip
    m = raw / "manifest.jsonl"

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(scan)), m), (_entry(str(born), sha256="b" * 64), m)],
        ocr_mode=True, ocr_fn=None,        # dry: no OCR
    ))
    assert stats.docs_ocr_candidate == 1
    assert stats.ocr_candidates["port-chester-rye"] == 1
    assert stats.docs_skipped == 1         # the born-digital one
    assert stats.chunks_written == 0       # nothing OCR'd or written
    report = render_report(stats, dry_run=True, ocr=True)
    assert "OCR candidates by district" in report and "port-chester-rye" in report


def test_ocr_mode_real_run_recovers_and_writes(tmp_path):
    # Reuse the fake DB/Voyage doubles from the ingest tests.
    from tests.test_ingest_schools import FakeConn, FakeVoyage

    raw = tmp_path / "data" / "raw"
    (raw / "port-chester-rye" / "agenda").mkdir(parents=True)
    scan = raw / "port-chester-rye" / "agenda" / "aa_scan.pdf"
    _blank_pdf(scan)
    m = raw / "manifest.jsonl"

    conn, voyage = FakeConn(), FakeVoyage()
    ocr_text = "1. Call to Order\nThe board convened at 7 PM. " * 20

    def fake_ocr(path):
        return ocr_mod.ExtractedText(text=ocr_text, page_count=4)

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(scan)), m)],
        conn=conn, voyage=voyage, ocr_mode=True, ocr_fn=fake_ocr,
    ))
    assert stats.docs_ingested == 1 and stats.chunks_written > 0
    assert any("insert into chunks" in sql for sql, _ in conn.many)
    marks = [p for sql, p in conn.calls if "update documents set" in sql]
    assert marks and marks[0][0] == "ingested"
