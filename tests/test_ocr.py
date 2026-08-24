"""Tests for the OCR fallback and OCR-mode ingest.

Tesseract's binary isn't available in unit tests, so ``pytesseract`` is
monkeypatched — we verify the rasterize→OCR→chunk→write *wiring*, not
Tesseract itself (its quality is validated by a real Actions run).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import io
import sys
import types
from pathlib import Path

import fitz
import pytest

from herald import ocr as ocr_mod
from herald.ingest_schools import ingest_manifests, render_report
from herald.scrape.models import DocType, ManifestEntry


def _text_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(36, 36, 560, 800), text)
    doc.save(str(path))
    doc.close()


def _blank_pdf(path: Path, pages: int = 1) -> None:
    # Pages with no text layer — stands in for a scanned/image PDF.
    doc = fitz.open()
    for _ in range(pages):
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
    # Documents are the wrong unit for pricing a per-page bill, so the free
    # dry run counts PAGES too — that is what lets it price the paid run.
    assert stats.ocr_candidate_pages["port-chester-rye"] == 1
    report = render_report(stats, dry_run=True, ocr=True)
    assert "OCR candidates" in report and "port-chester-rye" in report
    assert "page(s)" in report and "est. cost" in report
    assert "$" in report                   # a dry run now quotes the paid one


def test_upright_rotates_using_osd(monkeypatch):
    from PIL import Image

    mod = types.ModuleType("pytesseract")
    mod.Output = types.SimpleNamespace(DICT="dict")  # type: ignore[attr-defined]
    mod.image_to_osd = lambda img, output_type=None: {"rotate": 90}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", mod)

    img = Image.new("RGB", (200, 100), "white")   # landscape, "sideways"
    out = ocr_mod.upright(img)
    assert (out.width, out.height) == (100, 200)  # rotated upright


def test_upright_is_noop_without_tesseract(monkeypatch):
    from PIL import Image

    mod = types.ModuleType("pytesseract")

    def boom(*a, **k):
        raise RuntimeError("tesseract not installed")

    mod.Output = types.SimpleNamespace(DICT="dict")  # type: ignore[attr-defined]
    mod.image_to_osd = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", mod)

    img = Image.new("RGB", (200, 100), "white")
    assert ocr_mod.upright(img) is img          # best effort: unchanged


def test_encode_png_downscales_when_over_the_size_cap():
    # The elmsford budget page that 400'd: a render too big for the API's
    # image limit must come back downscaled rather than at full size.
    from PIL import Image

    img = Image.effect_noise((1200, 1200), 128).convert("RGB")
    full = io.BytesIO()
    img.save(full, format="PNG", optimize=True)

    data = ocr_mod._encode_png(img, max_bytes=50_000)
    assert len(data) < len(full.getvalue())      # downscaling kicked in


def test_encode_png_leaves_a_normal_page_full_size():
    from PIL import Image

    img = Image.new("RGB", (1700, 2200), "white")   # ordinary 200-dpi letter page
    data = ocr_mod._encode_png(img)
    assert len(data) <= ocr_mod._MAX_IMAGE_BYTES


def test_split_markdown_tables_separates_grid_from_prose():
    md = (
        "Article 12 — Salary\n"
        "The following schedule applies.\n"
        "| Step | BA | MA |\n"
        "| --- | --- | --- |\n"
        "| 1 | 55000 | 60000 |\n"
        "| 2 | 57000 | 62000 |\n"
        "\n"
        "Longevity is paid after 15 years."
    )
    prose, tables = ocr_mod.split_markdown_tables(md, page=7)
    assert len(tables) == 1
    assert tables[0].page == 7
    assert "| 1 | 55000 | 60000 |" in tables[0].markdown
    assert "Article 12" in prose and "Longevity" in prose
    assert "55000" not in prose               # the grid was lifted out of prose


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeStream:
    def __init__(self, msg) -> None:
        self._msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._msg


class _FakeMessages:
    def __init__(self, md: str, stop_reason: str = "end_turn",
                 usage: tuple[int, int] | None = (2500, 1500)) -> None:
        self._md = md
        self._stop = stop_reason
        self._usage = usage
        self.calls = 0
        self.max_tokens: int | None = None

    def stream(self, **kwargs):
        self.calls += 1
        self.max_tokens = kwargs.get("max_tokens")
        msg = types.SimpleNamespace(
            content=[_FakeBlock(self._md)], stop_reason=self._stop,
        )
        if self._usage is not None:
            msg.usage = types.SimpleNamespace(
                input_tokens=self._usage[0], output_tokens=self._usage[1],
            )
        return _FakeStream(msg)


class _FakeAnthropic:
    def __init__(self, md: str, stop_reason: str = "end_turn",
                 usage: tuple[int, int] | None = (2500, 1500)) -> None:
        self.messages = _FakeMessages(md, stop_reason, usage)


def test_ocr_pdf_vision_returns_tables_from_markdown(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)  # 1 page; vision doesn't care about the text layer
    client = _FakeAnthropic(
        "Salary Schedule 2024-25\n| Step | MA+30 |\n| --- | --- |\n| 1 | 82000 |"
    )
    doc = ocr_mod.ocr_pdf_vision(pdf, client=client, model="m", dpi=72)
    assert client.messages.calls == 1          # one call per page
    assert doc.page_count == 1
    assert len(doc.tables) == 1 and "82000" in doc.tables[0].markdown
    assert "Salary Schedule 2024-25" in doc.text


def test_transcription_budget_is_generous_and_truncation_is_loud(tmp_path, caplog):
    # The silent failure that cost us Tarrytown's salary grid: max_tokens caps
    # thinking + output together on this model, a dense grid exhausted the old
    # 8000 on thinking, and the truncated (empty) result was never flagged.
    import logging

    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)
    client = _FakeAnthropic("partial…", stop_reason="max_tokens")
    with caplog.at_level(logging.WARNING, logger="herald.ocr"):
        ocr_mod.ocr_pdf_vision(pdf, client=client, model="m", dpi=72)

    assert client.messages.max_tokens >= 32000        # real headroom
    assert any("truncated" in r.message for r in caplog.records)


def test_vision_run_measures_what_it_spent(tmp_path):
    # A pilot run exists to price a big one. Until this landed, `usage` came
    # back on every response and was discarded, so the only available answer to
    # "what will 2,240 pages cost?" was an estimate.
    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf, pages=3)
    usage = ocr_mod.VisionUsage()
    client = _FakeAnthropic("some prose", usage=(2500, 1500))
    ocr_mod.ocr_pdf_vision(pdf, client=client, model="m", dpi=72, usage=usage)

    assert usage.pages == 3
    assert usage.input_tokens == 7500 and usage.output_tokens == 4500
    # 7500 in @ $2/MTok + 4500 out @ $10/MTok = $0.015 + $0.045
    assert usage.cost_usd(ocr_mod.VISION_RATES_INTRO) == pytest.approx(0.06)
    assert usage.per_page_usd(ocr_mod.VISION_RATES_INTRO) == pytest.approx(0.02)
    # and that measured rate is what prices the real run
    assert usage.project_usd(2240, ocr_mod.VISION_RATES_INTRO) == pytest.approx(44.80)


def test_intro_pricing_expires_on_its_own(tmp_path):
    # The rate is picked by date, so a run in September prices itself right
    # without anyone remembering to edit a constant.
    assert ocr_mod.vision_rates(_dt.date(2026, 8, 31)) == ocr_mod.VISION_RATES_INTRO
    assert ocr_mod.vision_rates(_dt.date(2026, 9, 1)) == ocr_mod.VISION_RATES_STANDARD


def test_truncated_pages_are_counted_not_just_logged(tmp_path):
    # A truncated page is incomplete content that still costs full price —
    # the spend report has to say so, not just warn into a log nobody reads.
    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf, pages=2)
    usage = ocr_mod.VisionUsage()
    client = _FakeAnthropic("partial…", stop_reason="max_tokens")
    ocr_mod.ocr_pdf_vision(pdf, client=client, model="m", dpi=72, usage=usage)

    assert usage.truncated_pages == 2
    from herald.ingest_schools import _vision_cost_note
    assert "TRUNCATED" in usage.summary()
    assert "truncated" in _vision_cost_note(usage)


def test_an_empty_run_costs_nothing_and_does_not_divide_by_zero():
    empty = ocr_mod.VisionUsage()
    assert empty.cost_usd() == 0.0 and empty.per_page_usd() == 0.0
    assert empty.summary() == "No pages transcribed."


def test_ocr_mode_vision_writes_table_chunks(tmp_path):
    # A vision ocr_fn returns an ExtractedDoc with a table -> a kind='table'
    # chunk is written, which is what herald-extract later reads.
    from tests.test_ingest_schools import FakeConn, FakeVoyage

    raw = tmp_path / "data" / "raw"
    (raw / "white-plains" / "contract").mkdir(parents=True)
    scan = raw / "white-plains" / "contract" / "aa_cba.pdf"
    _blank_pdf(scan)
    m = raw / "manifest.jsonl"

    table_md = (
        "| Step | BA | MA | MA+30 |\n| --- | --- | --- | --- |\n"
        + "\n".join(f"| {s} | {50000 + s * 1500} | {55000 + s * 1600} | "
                    f"{60000 + s * 1700} |" for s in range(1, 12))
    )

    def fake_vision(path, pages=None):
        return ocr_mod.ExtractedDoc(
            text="Teacher salary schedule per the 2022-2025 agreement. " * 6,
            tables=[ocr_mod.TableBlock(page=1, markdown=table_md)],
            page_count=1,
        )

    conn, voyage = FakeConn(), FakeVoyage()
    stats = asyncio.run(ingest_manifests(
        [(_entry(str(scan), district="white-plains", doc_type=DocType.contract), m)],
        conn=conn, voyage=voyage, ocr_mode=True, ocr_fn=fake_vision,
    ))
    assert stats.docs_ingested == 1
    kinds = [p for sql, params in conn.many if "insert into chunks" in sql for p in params]
    assert any("table" in str(row) for row in kinds)   # a table chunk was written


def test_reocr_replaces_chunks_of_already_ingested_scan(tmp_path):
    # A scan OCR'd once (Tesseract, status='ingested') is re-OCR'd with --reocr:
    # its old chunks are deleted and the new (vision) chunks written, instead of
    # being skipped as already-ingested.
    from tests.test_ingest_schools import FakeConn, FakeVoyage

    raw = tmp_path / "data" / "raw"
    (raw / "tarrytowns" / "contract").mkdir(parents=True)
    scan = raw / "tarrytowns" / "contract" / "aa_tat.pdf"
    _blank_pdf(scan)
    m = raw / "manifest.jsonl"

    conn, voyage = FakeConn(), FakeVoyage()
    conn.existing_status = "ingested"      # already OCR'd once

    grid = "| Step | MA+30 |\n| --- | --- |\n| 1 | 82000 |\n| 2 | 84000 |"

    def fake_vision(path, pages=None):
        return ocr_mod.ExtractedDoc(
            text="Teacher salary schedule per the 2022-2025 agreement. " * 6,
            tables=[ocr_mod.TableBlock(page=1, markdown=grid)],
            page_count=1,
        )

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(scan), district="tarrytowns", doc_type=DocType.contract), m)],
        conn=conn, voyage=voyage, ocr_mode=True, ocr_fn=fake_vision, reocr=True,
    ))
    assert stats.docs_ingested == 1 and stats.docs_skipped == 0
    assert any("delete from chunks" in sql for sql, _ in conn.calls)  # replace, not append


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

    def fake_ocr(path, pages=None):
        return ocr_mod.ExtractedText(text=ocr_text, page_count=4)

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(scan)), m)],
        conn=conn, voyage=voyage, ocr_mode=True, ocr_fn=fake_ocr,
    ))
    assert stats.docs_ingested == 1 and stats.chunks_written > 0
    assert any("insert into chunks" in sql for sql, _ in conn.many)
    marks = [p for sql, p in conn.calls if "update documents set" in sql]
    assert marks and marks[0][0] == "ingested"


def _mixed_pdf(path, *, text_pages=2, image_pages=(3,)):
    """A born-digital PDF with a flat image pasted on some pages.

    The White Plains CBA shape: plenty of text overall, so the document-level
    "is this scanned?" gate never fires, but the salary grids are images.
    """
    import fitz

    doc = fitz.open()
    total = text_pages + len(image_pages)
    img_png = _noise_png(600, 800)
    for pno in range(1, total + 1):
        page = doc.new_page(width=612, height=792)
        if pno in image_pages:
            page.insert_image(fitz.Rect(20, 20, 592, 772), stream=img_png)
        else:
            page.insert_text((72, 100), "Article " + str(pno) + ". " + ("Terms follow. " * 40))
    doc.save(str(path))
    doc.close()
    return path


def _noise_png(w, h):
    import io as _io

    from PIL import Image

    img = Image.new("L", (w, h), 220)
    for x in range(0, w, 7):          # some ink so it is not a blank image
        for y in range(0, h, 11):
            img.putpixel((x, y), 30)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_image_only_pages_finds_the_scan_inside_a_readable_pdf(tmp_path):
    from herald.pdf_text import extract_pdf, image_only_pages

    pdf = _mixed_pdf(tmp_path / "cba.pdf", text_pages=2, image_pages=(3, 4))
    # the document as a whole reads fine — this is why nobody noticed
    assert extract_pdf(pdf).content_chars > 200
    assert image_only_pages(pdf) == [3, 4]


def test_a_text_page_with_a_letterhead_is_not_an_image_page(tmp_path):
    import fitz

    from herald.pdf_text import image_only_pages

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(20, 20, 120, 80), stream=_noise_png(100, 60))  # small logo
    page.insert_text((72, 200), "Real text on the page. " * 30)
    pdf = tmp_path / "letterhead.pdf"
    doc.save(str(pdf))
    doc.close()
    assert image_only_pages(pdf) == []


def test_a_blank_page_is_not_an_image_page(tmp_path):
    import fitz

    from herald.pdf_text import image_only_pages

    doc = fitz.open()
    doc.new_page(width=612, height=792)
    pdf = tmp_path / "blank.pdf"
    doc.save(str(pdf))
    doc.close()
    assert image_only_pages(pdf) == []


def test_rendering_only_the_scanned_pages_skips_the_rest(tmp_path):
    from herald.ocr import _render_pages

    pdf = _mixed_pdf(tmp_path / "cba.pdf", text_pages=3, image_pages=(4, 5))
    rendered, page_count = _render_pages(pdf, dpi=50, max_pages=None, only=[4, 5])
    assert [p for p, _ in rendered] == [4, 5]
    assert page_count == 5              # true length, not the rendered count


def test_merge_keeps_the_born_digital_text_and_adds_the_recovered_grid():
    from herald.pdf_text import ExtractedDoc, TableBlock, merge_extracted

    base = ExtractedDoc(text="Article 1. Terms.", tables=[], page_count=70)
    add = ExtractedDoc(text="", tables=[TableBlock(page=49, markdown="| BA | MA |")],
                       page_count=1)
    merged = merge_extracted(base, add)
    assert "Article 1. Terms." in merged.text
    assert [t.page for t in merged.tables] == [49]
    assert merged.page_count == 70      # the base document's length wins


def test_partial_ocr_recovers_a_grid_from_a_document_that_reads_fine(tmp_path):
    # The White Plains failure: a born-digital CBA whose salary schedules are
    # flat images. It sails past the document-level "is this scanned?" gate,
    # so before --partial its grids were simply absent from the corpus and
    # nothing reported a problem.
    from tests.test_ingest_schools import FakeConn, FakeVoyage

    raw = tmp_path / "data" / "raw"
    (raw / "white-plains" / "contract").mkdir(parents=True)
    pdf = raw / "white-plains" / "contract" / "aa_cba.pdf"
    _mixed_pdf(pdf, text_pages=3, image_pages=(4, 5))
    m = raw / "manifest.jsonl"
    entry = _entry(str(pdf))

    asked: list = []

    def fake_ocr(path, pages=None):
        asked.append(pages)
        return ocr_mod.ExtractedDoc(
            text="",
            tables=[ocr_mod.TableBlock(page=p, markdown="| Step | BA | MA |\n|---|---|---|\n"
                                                        "| 1 | 63,541 | 68,000 |")
                    for p in (pages or [])],
            page_count=len(pages or []),
        )

    conn, voyage = FakeConn(), FakeVoyage()
    stats = asyncio.run(ingest_manifests(
        [(entry, m)], conn=conn, voyage=voyage,
        ocr_mode=True, ocr_fn=fake_ocr, partial_ocr=True,
    ))

    assert asked == [[4, 5]]              # only the scanned pages were paid for
    assert stats.docs_partial_ocr == 1
    assert stats.docs_ingested == 1
    assert stats.docs_skipped == 0        # NOT written off as "has-text"


def test_without_partial_the_same_document_is_still_skipped(tmp_path):
    # Guards the default: page-level OCR costs money, so it is opt-in, and a
    # normal OCR pass must keep ignoring documents that already have text.
    from tests.test_ingest_schools import FakeConn, FakeVoyage

    raw = tmp_path / "data" / "raw"
    (raw / "white-plains" / "contract").mkdir(parents=True)
    pdf = raw / "white-plains" / "contract" / "aa_cba.pdf"
    _mixed_pdf(pdf, text_pages=3, image_pages=(4, 5))

    def fake_ocr(path, pages=None):
        raise AssertionError("should not be called")

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(pdf)), raw / "manifest.jsonl")],
        conn=FakeConn(), voyage=FakeVoyage(),
        ocr_mode=True, ocr_fn=fake_ocr, partial_ocr=False,
    ))
    assert stats.docs_skipped == 1 and stats.docs_partial_ocr == 0


def test_dry_run_prices_a_partial_pass_without_spending_anything(tmp_path):
    # The question "what will this cost?" is answerable for free: the dry run
    # already runs image_only_pages, so it knows the exact page count a paid
    # run would transcribe. Counting documents alone could not price it — one
    # 56-page budget deck costs what fifty one-page policies cost.
    from PIL import Image

    raw = tmp_path / "data" / "raw"
    (raw / "ossining" / "budget").mkdir(parents=True)
    deck = raw / "ossining" / "budget" / "aa_deck.pdf"

    # A "slide deck": page 1 is text, pages 2-4 are full-page chart images.
    doc = fitz.open()
    doc.new_page().insert_textbox(
        fitz.Rect(36, 36, 560, 800), "Directors' Budget Presentation. " * 40)
    buf = io.BytesIO()
    Image.effect_noise((600, 800), 64).convert("RGB").save(buf, format="PNG")
    for _ in range(3):
        page = doc.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(str(deck))
    doc.close()

    stats = asyncio.run(ingest_manifests(
        [(_entry(str(deck), district="ossining", doc_type=DocType.budget),
          raw / "manifest.jsonl")],
        ocr_mode=True, ocr_fn=None, partial_ocr=True,   # dry + partial
    ))

    assert stats.docs_ocr_candidate == 1
    assert stats.ocr_candidate_pages["ossining"] == 3   # the image pages only
    assert stats.chunks_written == 0                    # nothing spent, nothing written

    report = render_report(stats, dry_run=True, ocr=True)
    assert "3 page(s)" in report
    assert "ossining" in report
    assert "estimate" in report.lower()   # never passed off as a measurement
