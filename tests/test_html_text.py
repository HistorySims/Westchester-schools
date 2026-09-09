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


# --- BoardDocs agenda tables ------------------------------------------------

def _agenda_item(subject: str, body: str) -> str:
    """One BoardDocs agenda item: a Subject/dd pair, then the item body."""
    return (
        f'<dl class="row"><dt class="col leftcol">Subject</dt>'
        f'<dd class="col rightcol">{subject}</dd></dl>'
        f'<dl class="row"><dt class="col leftcol">Type</dt>'
        f'<dd class="col rightcol">Action (Consent)</dd></dl>'
        f'<div class="itembody">{body}</div>'
    )


def test_split_header_and_body_tables_are_rejoined() -> None:
    """Word pastes emit the header and the rows as separate <table>s.

    Port Chester's 2026-02-26 conference list arrives exactly this way. Left
    alone it yields one chunk of pure noise (a header with no rows) and one
    chunk of names and amounts whose columns are anonymous.
    """
    html = "<html><body>" + _agenda_item(
        "9.4 Conference(s)",
        "<p>RESOLVED, that the Board approves the following:</p>"
        "<table><tr><td>Name</td><td>Conference</td><td>Amount</td></tr></table>"
        "<table>"
        "<tr><td>Samantha Calvert</td><td>The Writing Revolution</td><td>$1,200.00</td></tr>"
        "<tr><td>Timothy Hartnett</td><td>NY Inspires</td><td>$2,100.00</td></tr>"
        "</table>",
    ) + "</body></html>"

    doc = extract_html_text(html)
    assert len(doc.tables) == 1                       # not two
    md = doc.tables[0].markdown
    assert md.splitlines()[0] == "| Name | Conference | Amount |"
    assert "Samantha Calvert" in md and "Timothy Hartnett" in md
    # The label is what makes the grid findable as conference travel.
    assert doc.tables[0].label == "9.4 Conference(s)"


def test_header_only_table_with_no_body_is_dropped() -> None:
    """A header with nothing under it is a retrievable piece of nothing."""
    html = "<html><body>" + _agenda_item(
        "9.4 Conference(s)",
        "<table><tr><td>Name</td><td>Conference</td><td>Amount</td></tr></table>",
    ) + "</body></html>"
    assert extract_html_text(html).tables == []


def test_two_real_tables_under_one_item_stay_separate() -> None:
    """The merge rule keys on a single-row block, so real grids never glue."""
    html = "<html><body>" + _agenda_item(
        "8.3 Competitive Bid(s)",
        "<table>"
        "<tr><td>Award To</td><td>Amount</td></tr>"
        "<tr><td>Better Speech, LLC</td><td>Various</td></tr>"
        "</table>"
        "<table>"
        "<tr><td>Vendor</td><td>Rate</td></tr>"
        "<tr><td>Carver Center</td><td>37,500</td></tr>"
        "</table>",
    ) + "</body></html>"
    assert len(extract_html_text(html).tables) == 2


def test_tables_under_different_items_are_never_merged() -> None:
    """A bare header does not absorb the next item's table."""
    html = "<html><body>" + _agenda_item(
        "9.4 Conference(s)",
        "<table><tr><td>Name</td><td>Conference</td><td>Amount</td></tr></table>",
    ) + _agenda_item(
        "9.6 Professional Services",
        "<table>"
        "<tr><td>Vendor</td><td>Function</td><td>Rate</td></tr>"
        "<tr><td>Carver Center</td><td>Smart Scholars</td><td>37,500</td></tr>"
        "</table>",
    ) + "</body></html>"
    tables = extract_html_text(html).tables
    assert len(tables) == 1                            # the bare header is gone
    assert tables[0].label == "9.6 Professional Services"
    assert "Name" not in tables[0].markdown


def test_table_label_falls_back_to_caption_then_heading() -> None:
    """Ordinary HTML has no BoardDocs Subject/dd pair."""
    capt = (
        "<html><body><table><caption>Fee Schedule</caption>"
        "<tr><td>Item</td><td>Fee</td></tr><tr><td>Transcript</td><td>$5</td></tr>"
        "</table></body></html>"
    )
    assert extract_html_text(capt).tables[0].label == "Fee Schedule"

    head = (
        "<html><body><h3>Salary Steps</h3><table>"
        "<tr><td>Lane</td><td>Step</td></tr><tr><td>MA</td><td>5</td></tr>"
        "</table></body></html>"
    )
    assert extract_html_text(head).tables[0].label == "Salary Steps"

    bare = (
        "<html><body><table>"
        "<tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr>"
        "</table></body></html>"
    )
    assert extract_html_text(bare).tables[0].label == ""
