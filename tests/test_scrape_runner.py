"""Tests for the batch crawl layer (targets + multi-committee crawl)."""

from __future__ import annotations

from pathlib import Path

from herald.scrape.boarddocs import BoardDocsClient, Committee, select_committees
from herald.scrape.core import Fetcher, Manifest, RawStore
from herald.scrape.runner import crawl_target, load_targets

FIXTURES = Path(__file__).parent / "fixtures" / "boarddocs"
REPO = Path(__file__).resolve().parents[1]
BASE = "https://go.boarddocs.com/ny/scarsdale/Board.nsf"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fast_fetcher() -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0)


# ---- targets file ---------------------------------------------------------


def test_load_targets_from_repo_file():
    targets = load_targets(REPO / "data" / "targets" / "port_chester_peers.json")
    assert any("Port Chester" in t.name for t in targets)
    assert {t.state for t in targets} == {"ny"}
    # every target carries a slug to confirm
    assert all(t.slug for t in targets)


# ---- committee selection --------------------------------------------------


def _committees() -> list[Committee]:
    return [
        Committee("A", "Board of Education"),
        Committee("B", "Policies"),
        Committee("C", "Audit Committee"),
    ]


def test_select_committees_by_match():
    picked = select_committees(_committees(), match="board|polic")
    assert {c.unique for c in picked} == {"A", "B"}


def test_select_committees_explicit_ids_override_match():
    picked = select_committees(_committees(), match="board", explicit_ids=["C"])
    assert [c.unique for c in picked] == ["C"]


def test_select_committees_none_returns_all():
    assert len(select_committees(_committees())) == 3


# ---- crawl_target end to end (mocked) -------------------------------------


def _mock_district(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/BD-GetCommittees?open", text=_load("committees.json"), is_reusable=True
    )
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


def test_crawl_target_selects_committees_and_downloads(httpx_mock, tmp_path):
    from herald.scrape.runner import Target

    _mock_district(httpx_mock)
    manifest = Manifest(tmp_path / "manifest.jsonl")
    store = RawStore(tmp_path / "raw")
    target = Target(district="scarsdale", name="Scarsdale", state="ny", slug="scarsdale")

    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f)
        per_committee = crawl_target(
            client, target, store=store, manifest=manifest,
            committee_match="board|polic", limit=1,
        )

    # "Board of Education" + "Policies" matched; "Audit Committee" excluded.
    assert set(per_committee) == {"Board of Education", "Policies"}
    # 3 unique files total; the second committee sees the same URLs -> deduped.
    total_downloaded = sum(s.downloaded for s in per_committee.values())
    assert total_downloaded == 3
    assert len(manifest.entries()) == 3
