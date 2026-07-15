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


def test_parse_committee_id_from_public_html():
    html = '<html><script> var current_committee_id = "A1B2C3D4E5"; </script></html>'
    assert parse_committee_id(html) == "A1B2C3D4E5"
    assert parse_committee_id("<html>no id here</html>") is None


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
        url=f"{BASE}/BD-GetAgenda?open", text=_load("agenda.html"), is_reusable=True
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
    # one meeting * three attachments
    assert len(docs) == 3
    by_type = {d.doc_type for d in docs}
    assert by_type == {DocType.minutes, DocType.policy, DocType.handbook}
    assert all(d.committee == "Board of Education" for d in docs)
    assert all(d.meeting_id == "MEET20240115" for d in docs)
    assert all(d.date == date(2024, 1, 15) for d in docs)


def test_iter_documents_since_filter(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json")
    )
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetAgenda?open", text=_load("agenda.html"), is_reusable=True
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
        r for r in httpx_mock.get_requests() if r.url.path.endswith("BD-GetAgenda")
    ]
    assert len(agenda_calls) == 2


# ---- runner: download + idempotency --------------------------------------


def _mock_full_crawl(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetMeetingsList?open", text=_load("meetings.json"), is_reusable=True
    )
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetAgenda?open", text=_load("agenda.html"), is_reusable=True
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

    assert stats.downloaded == 3
    entries = manifest.entries()
    assert len(entries) == 3
    # files really landed on disk under district/doc_type/
    for e in entries:
        assert Path(e.local_path).read_bytes().startswith(b"%PDF")
    types = {e.doc_type for e in entries}
    assert types == {DocType.minutes, DocType.policy, DocType.handbook}


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
    assert first.downloaded == 3

    # Second run with a fresh Manifest that reloads the prior state.
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f, prime_session=False)
        second = download_docs(
            iter_documents(client, district="scarsdale", committee="AAAA1111", limit=1),
            fetcher=f, store=store, manifest=Manifest(mpath),
        )
    assert second.downloaded == 0
    assert second.skipped_seen == 3
    assert len(Manifest(mpath).entries()) == 3  # no duplicate rows
