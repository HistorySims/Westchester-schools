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
