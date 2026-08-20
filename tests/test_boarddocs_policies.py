"""Tests for the BoardDocs policy console.

Fixtures are trimmed from the live responses of the four peer districts whose
manual lives here rather than on an external portal (Tarrytowns, White Plains,
Mount Vernon, Greenburgh Central).
"""

from __future__ import annotations

import pytest

from herald.scrape.boarddocs import (
    POLICY_STATUS,
    PolicyAccessDenied,
    PolicyItem,
    PolicyRef,
    choose_policy_books,
    parse_policy_books,
    parse_policy_files,
    parse_policy_item,
    parse_policy_list,
)

BOOKS = """
<div id="wrap-policy-book-select" style="display: none;">
<div class="dropdown">
<label for="book-menu">Book:</label><button id="book-menu">In Progress</button>
<ul class="dropdown-menu" id="policy-book-select">
<li><a class="dropdown-item" aria-label="In Progress" href="#">In Progress</a></li>
<li><a class="dropdown-item" aria-label="Policy Manual" href="#">Policy Manual</a></li>
<li><a class="dropdown-item" aria-label="Renumbering" href="#">Renumbering</a></li>
</ul></div></div>
"""

# NB: BoardDocs emits `unique= "..."` with a space after the equals sign.
INDEX = """
<div id="policy-accordion">
<section role="heading" key=Policy Manual>
  <a class="lefMenu">0000 Mission, Vision, Goals</a></section>
<div key=Policy Manual>
<a unique= "CL2PRY63FA74" class="icon prevnext policy" href="#" id="policy-CL2PRY63FA74">
<div><b>0100</b></div><div>Equal Opportunity And Nondiscrimination
  <div class="icons"></div></div></a>
<a unique= "CL2PZX649A75" class="icon prevnext policy" href="#" id="policy-CL2PZX649A75">
<div><b>0100-R</b></div><div>Equal Opportunity And Nondiscrimination Regulation</div></a>
</div>
<section role="heading" key=Policy Manual><a class="lefMenu">5000 Students</a></section>
<div key=Policy Manual>
<a unique= "BEV2PX03359D" class="icon prevnext policy" href="#" id="policy-BEV2PX03359D">
<div><b>5100</b></div><div>Attendance This Policy Contains an Attachment.</div></a>
<a class="icon notapolicy" href="#">not a policy row</a>
</div>
</div>
"""

ITEM = """
<div id="wrap-policy-item">
<div class="content-navigation"><button class="print">Print</button></div>
<div id="view-policy-item">
<div class="container">
<div class="row"><div class="col leftcol">Book</div>
  <div class="col rightcol">Policy Manual</div></div>
<div class="row"><div class="col leftcol">Section</div>
  <div class="col rightcol">0000 Mission, Vision, Goals</div></div>
<div class="row"><div class="col leftcol">Title</div>
  <div class="col rightcol">Equal Opportunity And Nondiscrimination</div></div>
<div class="row"><div class="col leftcol">Code</div><div class="col rightcol">0100</div></div>
<div class="row"><div class="col leftcol">Status</div><div class="col rightcol">Active</div></div>
<div class="row"><div class="col leftcol">Adopted</div>
  <div class="col rightcol">November 3, 2022</div></div>
<div class="row"><div class="col leftcol">Last Reviewed</div>
  <div class="col rightcol">November 3, 2022</div></div>
<div id="forcopy" class="bothcols DefaultStyle" key="publicbody">
  <p>The Board of Education shall not discriminate.</p></div>
</div></div></div>
"""


def test_parse_policy_books():
    assert parse_policy_books(BOOKS) == ["In Progress", "Policy Manual", "Renumbering"]


def test_a_district_with_no_books_is_a_real_answer_not_a_failure():
    # Port Chester, Peekskill, Ossining and Elmsford return an empty body
    # here because their manual is on an external portal.
    assert parse_policy_books("") == []


def test_choose_policy_books_prefers_the_adopted_manual_over_drafts():
    # "In Progress" holds policies mid-amendment; answering from those would
    # tell a parent what the district is *considering*, not what binds it.
    assert choose_policy_books(["In Progress", "Policy Manual", "Renumbering"]) \
        == ["Policy Manual"]
    assert choose_policy_books(["Board of Education Policies"]) == ["Board of Education Policies"]
    assert choose_policy_books(["Board of Education Policy Manual"]) \
        == ["Board of Education Policy Manual"]
    # never silently skip a district with an oddly named book
    assert choose_policy_books(["Green Book"]) == ["Green Book"]
    assert choose_policy_books([]) == []


def test_parse_policy_list_keeps_code_section_and_id():
    refs = parse_policy_list(INDEX, book="Policy Manual")
    assert [r.code for r in refs] == ["0100", "0100-R", "5100"]
    assert refs[0].unique == "CL2PRY63FA74"
    assert refs[0].title == "Equal Opportunity And Nondiscrimination"
    assert refs[0].section == "0000 Mission, Vision, Goals"
    assert refs[2].section == "5000 Students"          # heading above it, not the first
    assert all(r.book == "Policy Manual" for r in refs)


def test_the_attachment_marker_is_not_part_of_the_title():
    refs = parse_policy_list(INDEX, book="Policy Manual")
    assert refs[2].title == "Attendance"


def test_no_access_is_raised_not_returned_as_an_empty_manual():
    # This string is what every status value except "active" returns. Silently
    # treating it as "this district has no policies" is how the corpus grew a
    # hole in the first place.
    with pytest.raises(PolicyAccessDenied) as exc:
        parse_policy_list("No Access", book="Policy Manual")
    assert POLICY_STATUS in str(exc.value)


def test_parse_policy_item_captures_the_dates_the_portals_never_state():
    item = parse_policy_item(ITEM, unique="CL2PRY63FA74")
    assert item.code == "0100"
    assert item.title == "Equal Opportunity And Nondiscrimination"
    assert item.book == "Policy Manual"
    assert item.section == "0000 Mission, Vision, Goals"
    assert item.status == "Active"
    assert item.adopted == "November 3, 2022"
    assert item.last_reviewed == "November 3, 2022"
    assert "shall not discriminate" in item.body_html
    assert item.display_title == "0100 Equal Opportunity And Nondiscrimination"


def test_policy_item_falls_back_to_the_index_row_when_metadata_is_missing():
    ref = PolicyRef(unique="X1", code="5100", title="Attendance", section="5000", book="Manual")
    item = parse_policy_item("<div id='forcopy'><p>body</p></div>", unique="X1", fallback=ref)
    assert (item.code, item.title, item.section, item.book) == (
        "5100", "Attendance", "5000", "Manual",
    )


# Mount Vernon 0115 (DASA): an empty body, with the whole policy in two files.
FILES = (
    '<a class="public-file" target="_blank" order="00001" unique="CNUKKJ523D44"'
    ' href="/ny/mvcsd/Board.nsf/pfiles/CNUKKJ523D44/$file/0115%20DASA%202.23.pdf">'
    "0115 DASA 2.23.pdf</a>"
    '<a class="public-file" unique="CNUKKJ523D45"'
    ' href="/ny/mvcsd/Board.nsf/pfiles/CNUKKJ523D45/$file/0115-R%20DASA.pdf">reg</a>'
    '<a href="/ny/mvcsd/Board.nsf/Public">not a file</a>'
)
BASE = "https://go.boarddocs.com/ny/mvcsd/Board.nsf"


def test_the_index_flags_policies_whose_text_is_in_a_file():
    refs = parse_policy_list(INDEX, book="Policy Manual")
    assert [r.has_attachment for r in refs] == [False, False, True]


def test_parse_policy_files_returns_absolute_urls_and_skips_page_links():
    files = parse_policy_files(FILES, base_url=BASE)
    assert len(files) == 2
    assert files[0].url == (
        "https://go.boarddocs.com/ny/mvcsd/Board.nsf/pfiles/CNUKKJ523D44/"
        "$file/0115%20DASA%202.23.pdf"
    )
    assert files[1].url.endswith("$file/0115-R%20DASA.pdf")
    assert not any("Public" in f.url for f in files)


def test_an_empty_body_is_not_a_policy():
    # Storing the empty <div id="forcopy"> shell would put a zero-length
    # "policy" in the corpus; the real text is in the attachment.
    empty = PolicyItem(unique="X", code="0115", title="DASA",
                       body_html='<div id="forcopy" key="publicbody"></div>')
    assert empty.has_body is False
    assert PolicyItem(unique="X", code="0115", title="DASA",
                      body_html="<p>Real text.</p>").has_body is True


def test_manifest_dedupe_keeps_two_policies_that_share_a_body(tmp_path):
    # White Plains has two policies with byte-identical text. Deduping on
    # content alone dropped one of them — and its number and permalink with it.
    from datetime import UTC, datetime

    from herald.scrape.core import Manifest
    from herald.scrape.models import DocType, ManifestEntry

    m = Manifest(tmp_path / "manifest.jsonl")

    def entry(url: str, sha: str) -> ManifestEntry:
        return ManifestEntry(
            district="white-plains", doc_type=DocType.policy, title="t",
            source_url=url, local_path="p", sha256=sha, size_bytes=1,
            fetched_at=datetime.now(UTC),
        )

    m.append(entry("https://d.test/goto?id=A", "samehash"))
    assert m.has_hash("samehash")                                  # content seen…
    assert not m.has_url_hash("https://d.test/goto?id=B", "samehash")  # …but not here
    assert m.has_url_hash("https://d.test/goto?id=A", "samehash")      # true re-run

    m.append(entry("https://d.test/goto?id=B", "samehash"))
    # both survive a reload from disk
    assert Manifest(tmp_path / "manifest.jsonl").has_url_hash(
        "https://d.test/goto?id=B", "samehash"
    )


def test_an_amended_policy_is_not_skipped_as_already_seen(tmp_path):
    from datetime import UTC, datetime

    from herald.scrape.core import Manifest
    from herald.scrape.models import DocType, ManifestEntry

    m = Manifest(tmp_path / "manifest.jsonl")
    m.append(ManifestEntry(
        district="tarrytowns", doc_type=DocType.policy, title="5100",
        source_url="https://d.test/goto?id=A", local_path="p", sha256="v1",
        size_bytes=1, fetched_at=datetime.now(UTC),
    ))
    # same permalink, new text after the board amends it
    assert not m.has_url_hash("https://d.test/goto?id=A", "v2")
