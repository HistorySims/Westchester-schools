"""Tests for HTML extraction — the policy manuals' native format."""

from __future__ import annotations

from bs4 import BeautifulSoup

from herald.html_text import extract_html, extract_html_text, table_to_markdown
from herald.ingest_schools import MIN_TEXT_CHARS, extract_document

POLICY = """
<html><head><title>x</title><style>p { color: red }</style>
<script>var noise = 1;</script></head>
<body>
  <div class="export-section" id="37843">
    <p class="section-title">5100 ATTENDANCE</p>
    <p>Regular school attendance is a major component of academic success.</p>
    <table>
      <tr><th>Grade</th><th>Allowed absences</th></tr>
      <tr><td>9-12</td><td>18 | full year</td></tr>
    </table>
    <p>Adoption date: April 18, 2023</p>
  </div>
</body></html>
"""


def test_scripts_and_styles_never_reach_the_text():
    doc = extract_html_text(POLICY)
    assert "var noise" not in doc.text
    assert "color: red" not in doc.text
    assert "Regular school attendance" in doc.text


def test_tables_come_out_whole_and_leave_the_prose():
    doc = extract_html_text(POLICY)
    assert len(doc.tables) == 1
    md = doc.tables[0].markdown
    assert md.splitlines()[0] == "| Grade | Allowed absences |"
    assert md.splitlines()[1] == "|---|---|"
    # a literal pipe in a cell would break the row it becomes
    assert "18 / full year" in md
    # the table's cells must not also be sitting in the prose
    assert "Allowed absences" not in doc.text
    assert "Adoption date: April 18, 2023" in doc.text


def test_ragged_rows_are_padded_not_dropped():
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    table = BeautifulSoup(html, "html.parser").find("table")
    md = table_to_markdown(table)
    assert md.splitlines()[-1] == "| d |  |  |"


def test_a_table_with_no_rows_is_not_a_table():
    assert table_to_markdown(BeautifulSoup("<table></table>", "html.parser").find("table")) == ""
    assert extract_html_text("<table></table>").tables == []


def test_blank_line_runs_are_collapsed():
    doc = extract_html_text("<div><p>one</p><div><div></div></div><p>two</p></div>")
    assert doc.text == "one\ntwo"


def test_extract_document_dispatches_on_suffix(tmp_path):
    p = tmp_path / "5100.html"
    p.write_text(POLICY, encoding="utf-8")
    doc = extract_document(p)
    assert "Regular school attendance" in doc.text
    assert len(doc.tables) == 1
    assert extract_html(p).text == doc.text


def test_short_policies_survive_the_pdf_scanned_page_threshold(tmp_path):
    # "9100 STAFF ETHICS" is 143 characters of real, adopted policy. The
    # 200-char floor exists to catch scanned PDFs; applying it to HTML would
    # drop this and answer "no such policy".
    p = tmp_path / "9100.html"
    p.write_text("<p>The Board expects staff to hold themselves to a high "
                 "ethical standard in the conduct of district business.</p>", encoding="utf-8")
    doc = extract_document(p)
    assert 0 < doc.content_chars < MIN_TEXT_CHARS


def test_docx_and_rtf_attachments_are_readable(tmp_path):
    # A policy's attachment is whatever the district uploaded, and some upload
    # the regulation as Word or RTF. PyMuPDF cannot open either, so without a
    # reader the policy is in the corpus by title with no text behind it.
    import zipfile

    docx = tmp_path / "5830-R.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org'
            '/wordprocessingml/2006/main"><w:body>'
            "<w:p><w:r><w:t>Employees shall be reimbursed for </w:t></w:r>"
            "<w:r><w:t>actual expenses.</w:t></w:r></w:p>"
            "<w:p><w:r><w:t> </w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Receipts are required.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    doc = extract_document(docx)
    # runs inside a paragraph join; blank paragraphs drop out
    assert doc.text == (
        "Employees shall be reimbursed for actual expenses.\nReceipts are required."
    )

    rtf = tmp_path / "7530-R.rtf"
    rtf.write_text(
        r"{\rtf1\ansi{\fonttbl{\f0 Times;}}{\colortbl;\red0\green0\blue0;}"
        r"\f0\fs24 Child abuse must be reported\par within 24 hours.\par "
        r"An em dash \u8212?- ends this.}",
        encoding="latin-1",
    )
    text = extract_document(rtf).text
    assert "Child abuse must be reported" in text
    assert "within 24 hours." in text
    assert "Times" not in text              # font table dropped, not read as prose
    assert "red0" not in text               # colour table too
    assert "\u2014- ends this." in text      # \uN escape decoded, fallback char dropped
