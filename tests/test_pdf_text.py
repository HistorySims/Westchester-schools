"""PDF table captions: the line above a grid is what dates it."""

from pathlib import Path

import fitz

from herald.pdf_text import _caption_above, extract_pdf


def _grid_page(doc: fitz.Document, caption: str, first_salary: int) -> None:
    """One page: a caption line, then a ruled 3x3 step/lane grid beneath it."""
    page = doc.new_page()
    page.insert_text((100, 90), caption, fontsize=11)
    x0, y0, w, h = 100, 120, 120, 24
    rows = [["Step", "BA", "MA"],
            ["1", f"{first_salary:,}", f"{first_salary + 9000:,}"],
            ["2", f"{first_salary + 1800:,}", f"{first_salary + 10800:,}"]]
    for r in range(len(rows) + 1):
        page.draw_line((x0, y0 + r * h), (x0 + 3 * w, y0 + r * h))
    for c in range(4):
        page.draw_line((x0 + c * w, y0), (x0 + c * w, y0 + len(rows) * h))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            page.insert_text((x0 + c * w + 6, y0 + r * h + 16), cell, fontsize=10)


def test_each_year_grid_carries_its_caption(tmp_path: Path):
    # The Greenburgh / Ossining shape: one grid per page, the year ONLY in the
    # caption. Unlabelled, the four grids were indistinguishable and all got the
    # title's first year.
    pdf = tmp_path / "moa.pdf"
    doc = fitz.open()
    _grid_page(doc, "OTA 2025-2026 SALARY SCHEDULE", 60700)
    _grid_page(doc, "OTA 2026-2027 SALARY SCHEDULE", 61611)
    doc.save(pdf)
    doc.close()

    out = extract_pdf(pdf)
    assert [(t.page, t.label) for t in out.tables] == [
        (1, "OTA 2025-2026 SALARY SCHEDULE"),
        (2, "OTA 2026-2027 SALARY SCHEDULE"),
    ]


def test_caption_skips_images_blanks_far_text_and_paragraphs():
    table = fitz.Rect(100, 200, 500, 400)
    blocks = [
        (100, 60, 400, 80, "OTA 2025-2026 SALARY SCHEDULE", 0, 0),   # 120pt up: too far
        (100, 140, 400, 160, "APPENDIX III", 1, 0),                    # chosen: 40pt
        (100, 170, 400, 190, "   ", 2, 0),                             # blank text block
        (100, 175, 400, 195, "<image>", 3, 1),                         # image block
        (520, 180, 590, 195, "Page 56", 4, 0),                         # beside, not above
    ]
    assert _caption_above(table, blocks) == "APPENDIX III"
    long_para = (100, 170, 400, 195, "This Agreement " * 20, 5, 0)
    assert _caption_above(table, [*blocks, long_para]) == "APPENDIX III"
    assert _caption_above(table, []) == ""
