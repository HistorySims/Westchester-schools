"""Tests for the BoardDocs adapter (parsers + client + discovery)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from herald.scrape.boarddocs import (
    BoardDocsClient,
    analyze_public_html,
    classify_filename,
    iter_documents,
    parse_agenda_files,
    parse_committee_id,
    parse_committees,
    parse_meetings,
)
from herald.scrape.core import Fetcher, Manifest, RawStore
from herald.scrape.models import DocType
from herald.scrape.runner import download_docs

FIXTURES = Path(__file__).parent / "fixtures" / "boarddocs"
BASE = "https://go.boarddocs.com/ny/scarsdale/Board.nsf"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fast_fetcher() -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0, respect_robots=False)


# ---- pure parsers ---------------------------------------------------------


def test_parse_committees_from_public_html():
    # Mirrors the real /Public menu markup (bd.current_committee_id is empty).
    html = """
    <html><body>
      <script>bd.current_committee_id = ""; // not used</script>
      <li><a href="#" class="dropdown-item committee-trigger" committeeid="A4EP6J588C05"
             aria-label="Board of Education">Board of Education</a></li>
      <div id="committee-select"><select name="committeeid">
        <option value="A4EP6J588C05">Board of Education</option>
        <option value="BEKQYV6A6369">Policy Committee</option>
      </select></div>
    </body></html>
    """
    committees = parse_committees(html)
    by_id = {c.unique: c.name for c in committees}
    assert by_id == {
        "A4EP6J588C05": "Board of Education",
        "BEKQYV6A6369": "Policy Committee",
    }


def test_parse_committee_id_from_public_html():
    # JS var form
    assert parse_committee_id('var current_committee_id = "A1B2C3D4E5";') == "A1B2C3D4E5"
    # JSON/config form
    assert parse_committee_id('{"current_committee_id":"A1B2C3D4E5"}') == "A1B2C3D4E5"
    # URL / deep-link param form
    assert parse_committee_id("Public#&id=X&current_committee_id=A1B2C3D4E5&y=1") == "A1B2C3D4E5"
    assert parse_committee_id("<html>no committee here</html>") is None


def test_parse_meetings_reads_numberdate():
    meetings = parse_meetings(json.loads(_load("meetings.json")))
    assert meetings[0].unique == "MEET20240115"
    assert meetings[0].date == date(2024, 1, 15)
    assert meetings[1].date == date(2023, 12, 4)
    # empty/missing numberdate yields a None date, not a crash
    assert meetings[2].date is None


def test_parse_meetings_accepts_string_payload():
    # BoardDocs occasionally hands back a JSON string, not parsed JSON.
    meetings = parse_meetings(_load("meetings.json"))
    assert len(meetings) == 3


def test_parse_agenda_files_json():
    # Current BoardDocs returns JSON: agenda items with a nested files array.
    payload = json.dumps(
        [
            {
                "unique": "ITEM1",
                "name": "Approval of Minutes",  # an item, not a file (no ext)
                "files": [
                    {"unique": "FILE1", "name": "05-14-23_Minutes.pdf",
                     "description": "Approved Minutes"},
                ],
            },
            {"unique": "ITEM2", "name": "Policy 5030", "files": [
                {"unique": "FILE2", "name": "Policy-5030.pdf"},
            ]},
        ]
    )
    files = parse_agenda_files(payload, base_url=BASE)
    urls = {f.url for f in files}
    assert f"{BASE}/files/FILE1/$file/05-14-23_Minutes.pdf" in urls
    assert f"{BASE}/files/FILE2/$file/Policy-5030.pdf" in urls
    # the agenda items themselves (no file extension) are not treated as files
    assert len(files) == 2
    assert any(f.title == "Approved Minutes" for f in files)


def test_parse_agenda_files_filters_and_resolves():
    files = parse_agenda_files(_load("agenda.html"), base_url=BASE)
    urls = [f.url for f in files]
    # 3 unique document links; the "#section", "goto?open" and the duplicate
    # minutes link are all dropped.
    assert len(files) == 3
    assert f"{BASE}/files/ABC123/$file/Minutes-January-2024.pdf" in urls
    assert f"{BASE}/files/DEF456/$file/Policy-5030-Wellness.pdf" in urls
    assert f"{BASE}/files/GHI789/$file/Student-Handbook-2024.pdf" in urls
    # relative hrefs resolve against the site root
    assert all(u.startswith("https://go.boarddocs.com/") for u in urls)
    assert files[0].title == "January 2024 Meeting Minutes"


def test_analyze_public_html_extracts_scripts_and_committee_hints():
    html = """
    <html><head>
      <script src="/ny/scarsdale/Board.nsf/app.js"></script>
      <script src="https://cdn.example/lib.js"></script>
    </head><body>
      <input id="current_committee" value="A1B2C3D4">
      <div>Select a committee to view meetings</div>
    </body></html>
    """
    info = analyze_public_html(html, status=200)
    assert info.status == 200
    assert "/ny/scarsdale/Board.nsf/app.js" in info.script_srcs
    assert "https://cdn.example/lib.js" in info.script_srcs
    assert any("committee" in h.lower() for h in info.committee_hints)


def test_classify_filename():
    assert classify_filename("Minutes-January-2024.pdf") is DocType.minutes
    assert classify_filename("Policy 5030 Wellness") is DocType.policy
    assert classify_filename("Student-Handbook-2024.pdf") is DocType.handbook
    assert classify_filename("Agenda.pdf") is DocType.agenda
    assert classify_filename("random-attachment.pdf") is DocType.other


# ---- client (mocked network) ---------------------------------------------


def test_client_list_meetings(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json")
    )
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        meetings = client.list_meetings("AAAA1111")
    assert len(meetings) == 3
    req = httpx_mock.get_requests()[0]
    assert b"current_committee_id=AAAA1111" in req.content


def test_client_primes_session_and_sends_referer(httpx_mock):
    # With prime_session on (the default), the client loads the public page
    # first (to pick up cookies) and the AJAX POST carries Referer + Origin.
    httpx_mock.add_response(url=f"{BASE}/Public", text="<html>board</html>")
    httpx_mock.add_response(url=f"{BASE}/BD-GetMeetingsList?open", text="[]")
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f)  # prime_session=True
        client.list_meetings("X")
    reqs = httpx_mock.get_requests()
    assert reqs[0].url.path.endswith("/Board.nsf/Public")  # primed before the POST
    post = next(r for r in reqs if r.url.path.endswith("BD-GetMeetingsList"))
    assert post.headers.get("Referer", "").endswith("/Board.nsf/Public")
    assert post.headers.get("Origin") == "https://go.boarddocs.com"


def test_iter_documents_end_to_end(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json")
    )
    httpx_mock.add_response(
        url=f"{BASE}/PRINT-AgendaDetailed?open", text=_load("agenda.html"), is_reusable=True
    )
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        docs = list(
            iter_documents(
                client,
                district="scarsdale",
                committee="AAAA1111",
                committee_name="Board of Education",
                limit=1,  # walk only the newest meeting -> one agenda
            )
        )
    # one meeting: its own agenda, plus three attachments
    assert len(docs) == 4
    by_type = {d.doc_type for d in docs}
    assert by_type == {DocType.agenda, DocType.minutes, DocType.policy, DocType.handbook}
    assert sum(d.doc_type is DocType.agenda for d in docs) == 1
    assert all(d.committee == "Board of Education" for d in docs)
    assert all(d.meeting_id == "MEET20240115" for d in docs)
    assert all(d.date == date(2024, 1, 15) for d in docs)


def test_iter_documents_since_filter(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json")
    )
    httpx_mock.add_response(
        url=f"{BASE}/PRINT-AgendaDetailed?open", text=_load("agenda.html"), is_reusable=True
    )
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        list(
            iter_documents(
                client,
                district="scarsdale",
                committee="AAAA1111",
                since=date(2024, 1, 1),
            )
        )
    # meetings walked = those on/after 2024-01-01 OR undated (kept): 2 agendas
    agenda_calls = [
        r for r in httpx_mock.get_requests() if r.url.path.endswith("PRINT-AgendaDetailed")
    ]
    assert len(agenda_calls) == 2


# ---- runner: download + idempotency --------------------------------------


def _mock_full_crawl(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json"), is_reusable=True
    )
    httpx_mock.add_response(
        url=f"{BASE}/PRINT-AgendaDetailed?open", text=_load("agenda.html"), is_reusable=True
    )
    # the agenda itself is downloaded by GET now, not only its attachments
    httpx_mock.add_response(
        url=f"{BASE}/PRINT-AgendaDetailed?open&id=MEET20240115"
            f"&current_committee_id=AAAA1111",
        text=_load("agenda.html"),
        headers={"Content-Type": "text/html"},
        is_reusable=True,
    )
    files = {
        "ABC123": "Minutes-January-2024.pdf",
        "DEF456": "Policy-5030-Wellness.pdf",
        "GHI789": "Student-Handbook-2024.pdf",
    }
    for fid, fname in files.items():
        httpx_mock.add_response(
            url=f"{BASE}/files/{fid}/$file/{fname}",
            content=f"%PDF-1.4 {fid}".encode(),
            headers={"Content-Type": "application/pdf"},
            is_reusable=True,
        )


def test_download_docs_writes_manifest_and_files(httpx_mock, tmp_path):
    _mock_full_crawl(httpx_mock)
    store = RawStore(tmp_path / "raw")
    manifest = Manifest(tmp_path / "raw" / "manifest.jsonl")
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        docs = iter_documents(client, district="scarsdale", committee="AAAA1111", limit=1)
        stats = download_docs(docs, fetcher=f, store=store, manifest=manifest)

    assert stats.downloaded == 4      # 3 attachments + the meeting's own agenda
    entries = manifest.entries()
    assert len(entries) == 4
    types = {e.doc_type for e in entries}
    assert types == {DocType.agenda, DocType.minutes, DocType.policy, DocType.handbook}

    # files really landed on disk under district/doc_type/
    agenda = next(e for e in entries if e.doc_type is DocType.agenda)
    for e in entries:
        body = Path(e.local_path).read_bytes()
        assert body.startswith(b"%PDF") or e is agenda
    # the agenda is saved as HTML so ingest reads it with extract_html, not
    # PyMuPDF — which would open it as nothing and file it as no_text
    assert Path(agenda.local_path).suffix == ".html"


def test_download_docs_is_idempotent(httpx_mock, tmp_path):
    _mock_full_crawl(httpx_mock)
    store = RawStore(tmp_path / "raw")
    mpath = tmp_path / "raw" / "manifest.jsonl"

    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        first = download_docs(
            iter_documents(client, district="scarsdale", committee="AAAA1111", limit=1),
            fetcher=f, store=store, manifest=Manifest(mpath),
        )
    assert first.downloaded == 4      # 3 attachments + the agenda itself

    # Second run with a fresh Manifest that reloads the prior state.
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        second = download_docs(
            iter_documents(client, district="scarsdale", committee="AAAA1111", limit=1),
            fetcher=f, store=store, manifest=Manifest(mpath),
        )
    assert second.downloaded == 0
    assert second.skipped_seen == 4   # the agenda dedupes like any artifact
    assert len(Manifest(mpath).entries()) == 4  # no duplicate rows


# Trimmed from a live tufsd agenda (PRINT-AgendaDetailed). The shape that
# matters: attachments sit in div.print-files inside div.container.item
# .agendaorder, and the item's own heading is the first bold block.
AGENDA_WITH_ITEMS = """
<div tabindex="0" class="container item agendaorder">
  <div style="font-weight: bold;">Subject 9.2 Minutes Reorganization and Regular
     Meeting July 9, 2026</div>
  <div role="heading" class="print-files">
    <div>File Attachments</div>
    <div class="public-file print-file" unique="DWV1">
      <a target="_blank" href="/ny/tufsd/Board.nsf/files/DWV1/$file/Min%20Org%207.9.26.pdf">Min
         Org 7.9.26.pdf (3,798 KB)</a></div>
  </div>
</div>
<div tabindex="0" class="container item agendaorder">
  <div style="font-weight: bold;">Subject 6.3 Consideration for a Memorial Plaque</div>
  <div class="print-files">
    <div class="public-file"><a href="/ny/tufsd/Board.nsf/files/DWV2/$file/JP%20playground.pdf">JP
       playground dedication.pdf (2,916 KB)</a></div>
    <div class="public-file"><a href="/ny/tufsd/Board.nsf/files/DWV3/$file/Memo.pdf">Memo to
       BOE, memorial plaque.pdf (93 KB)</a></div>
  </div>
</div>
"""
TUFSD_BASE = "https://go.boarddocs.com/ny/tufsd/Board.nsf"


def test_an_attachment_carries_the_agenda_item_it_hangs_under():
    from herald.scrape.boarddocs import parse_agenda_files

    refs = parse_agenda_files(AGENDA_WITH_ITEMS, base_url=TUFSD_BASE)
    assert len(refs) == 3
    assert refs[0].item_title == (
        "Minutes Reorganization and Regular Meeting July 9, 2026"
    )
    # BoardDocs' own numbering chrome is not part of the subject
    assert not refs[0].item_title.lower().startswith("subject")
    # two files under one item both get it
    assert refs[1].item_title == refs[2].item_title == "Consideration for a Memorial Plaque"


def test_the_agenda_item_types_a_file_its_own_name_cannot():
    # "Min Org 7.9.26.pdf" defeats even the abbreviation rule — "min" is
    # followed by "org", not a date. The agenda item says "Minutes" outright.
    from herald.chunking import classify_doc_type
    from herald.scrape.boarddocs import parse_agenda_files

    ref = parse_agenda_files(AGENDA_WITH_ITEMS, base_url=TUFSD_BASE)[0]
    assert classify_doc_type(ref.title) == "other"
    assert classify_doc_type(f"{ref.item_title} {ref.title}") == "minutes"


def test_best_title_prefers_the_item_and_falls_back_to_the_filename():
    from herald.scrape.boarddocs import FileRef, parse_agenda_files

    ref = parse_agenda_files(AGENDA_WITH_ITEMS, base_url=TUFSD_BASE)[0]
    # a citation reads the subject, not "Min Org 7.9.26.pdf (3,798 KB)"
    assert ref.best_title.startswith("Minutes Reorganization")
    assert FileRef(url="u", title="only.pdf").best_title == "only.pdf"


def test_an_attachment_outside_an_agenda_item_still_parses():
    from herald.scrape.boarddocs import parse_agenda_files

    loose = '<a href="/ny/tufsd/Board.nsf/files/X/$file/loose.pdf">loose.pdf</a>'
    refs = parse_agenda_files(loose, base_url=TUFSD_BASE)
    assert len(refs) == 1 and refs[0].item_title == ""
    assert refs[0].best_title == "loose.pdf"


# Peekskill's real structure, trimmed: a pseudo-meeting named "2026 Minutes"
# dated 2026-12-31, holding one attachment per meeting of the year. Each
# attachment's own title names the MEETING it minutes, never "minutes".
PCSD_BASE = "https://go.boarddocs.com/ny/pcsd/Board.nsf"
MINUTES_COLLECTION_AGENDA = """
<div tabindex="0" class="container item agendaorder">
  <div style="font-weight: bold;">Subject B. Business Meeting July 28, 2026</div>
  <div class="print-files">
    <div class="public-file"><a
       href="/ny/pcsd/Board.nsf/files/DZZ1/$file/BM%207-28-26.pdf">BM 7-28-26.pdf</a></div>
  </div>
</div>
<div tabindex="0" class="container item agendaorder">
  <div style="font-weight: bold;">Subject A. Personnel Agenda</div>
  <div class="print-files">
    <div class="public-file"><a
       href="/ny/pcsd/Board.nsf/files/DZZ2/$file/Pers.pdf">Personnel Agenda.pdf</a></div>
  </div>
</div>
"""


def _minutes_collection_docs(meeting_name: str):
    """Run iter_documents over one meeting with the fixture above."""
    from herald.scrape.boarddocs import Meeting, parse_agenda_files

    meeting = Meeting(unique="M2026MIN", name=meeting_name, date=date(2026, 12, 31))
    refs = parse_agenda_files(MINUTES_COLLECTION_AGENDA, base_url=PCSD_BASE)

    class _Client:
        base_url = PCSD_BASE

        def list_meetings(self, committee):
            return [meeting]

        def get_agenda_files(self, m, committee):
            return refs

        def agenda_url(self, m, committee):
            return BoardDocsClient.agenda_url(self, m, committee)

    return list(iter_documents(_Client(), district="peekskill", committee="C1"))


def test_a_years_minutes_filed_under_one_meeting_are_typed_minutes():
    # The live failure: Peekskill showed 16 agendas and ZERO minutes while its
    # whole archive sat in a meeting called "2026 Minutes". Each attachment
    # reads "Business Meeting July 28, 2026" — no "minute" anywhere — so the
    # file classifier said 'other' and the ingest classifier then made it an
    # 'agenda', because "business meeting" is an agenda keyword.
    docs = _minutes_collection_docs("2026 Minutes")
    by_title = {d.title: d.doc_type for d in docs}

    business = next(t for t in by_title if "Business Meeting" in t)
    assert by_title[business] is DocType.minutes, (
        "the parent meeting is the only thing that says 'minutes'"
    )

    # ...but a file that names itself is taken at its word: only 'other' is
    # upgraded, so a Personnel Agenda inside the collection stays an agenda.
    personnel = next(t for t in by_title if "Personnel Agenda" in t)
    assert by_title[personnel] is DocType.agenda


def test_an_ordinary_meeting_does_not_relabel_its_attachments():
    # The upgrade must be driven by the meeting NAME, not by being in a
    # meeting at all — otherwise every attachment everywhere becomes minutes.
    docs = _minutes_collection_docs("BUSINESS MEETING/WORK SESSION")
    by_title = {d.title: d.doc_type for d in docs}
    business = next(t for t in by_title if "Business Meeting" in t)
    assert by_title[business] is DocType.other


def _agenda_docs(meeting_name: str, unique: str = "MEET1"):
    from herald.scrape.boarddocs import Meeting, parse_agenda_files

    meeting = Meeting(unique=unique, name=meeting_name, date=date(2026, 5, 28))
    refs = parse_agenda_files(MINUTES_COLLECTION_AGENDA, base_url=PCSD_BASE)

    class _Client:
        base_url = PCSD_BASE

        def list_meetings(self, committee):
            return [meeting]

        def get_agenda_files(self, m, committee):
            return refs

        def agenda_url(self, m, committee):
            return BoardDocsClient.agenda_url(self, m, committee)

    return list(iter_documents(_Client(), district="peekskill", committee="C1"))


def test_the_agenda_itself_is_captured_not_only_its_attachments():
    # The crawler fetched each agenda, scraped the attachment links out of it,
    # and discarded ~19,500 characters of itemised meeting content. Mount
    # Vernon and Greenburgh had zero agendas across hundreds of meetings
    # because no agenda was ever saved as a document.
    docs = _agenda_docs("Board of Education Meeting - 6:00 p.m.")
    agendas = [d for d in docs if d.doc_type is DocType.agenda
               and d.title.endswith("Agenda (2026-05-28)")]
    assert len(agendas) == 1, "the meeting's own agenda must be a document"

    a = agendas[0]
    # Downloadable by plain GET: the crawl reaches it by POST, which the
    # runner cannot repeat.
    assert a.source_url.startswith(f"{PCSD_BASE}/PRINT-AgendaDetailed?open&id=MEET1")
    assert "current_committee_id=C1" in a.source_url
    # .html so ingest dispatches to extract_html instead of PyMuPDF
    assert a.suggested_filename.endswith(".html")
    assert a.date == date(2026, 5, 28) and a.meeting_id == "MEET1"


def test_a_minutes_collection_contributes_no_phantom_agenda():
    # Peekskill's "2026 Minutes" is a container, not a meeting. Its rendered
    # agenda is just an index of the attachments, and saving it would invent a
    # board meeting on 2026-05-28 that never happened.
    docs = _agenda_docs("2026 Minutes")
    assert not [d for d in docs if d.title.endswith("Agenda (2026-05-28)")]
