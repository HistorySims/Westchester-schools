"""Tests for the batch crawl layer (targets + multi-committee crawl)."""

from __future__ import annotations

from pathlib import Path

from herald.scrape.boarddocs import BoardDocsClient
from herald.scrape.core import Fetcher, Manifest, RawStore
from herald.scrape.runner import (
    DistrictResult,
    ScrapeStats,
    crawl_target,
    load_targets,
    render_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "boarddocs"
REPO = Path(__file__).resolve().parents[1]
BASE = "https://go.boarddocs.com/ny/scarsdale/Board.nsf"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _fast_fetcher() -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0, respect_robots=False)


# ---- targets file ---------------------------------------------------------


def test_load_targets_from_repo_file():
    targets = load_targets(REPO / "data" / "targets" / "port_chester_peers.json")
    assert any("Port Chester" in t.name for t in targets)
    assert {t.state for t in targets} == {"ny"}
    # every target carries a slug to confirm
    assert all(t.slug for t in targets)


# ---- crawl_target end to end (mocked) -------------------------------------


def _mock_district(httpx_mock, *, committee_id: str = "COMM123") -> None:
    # /Public embeds the committee id; crawl_target discovers it there.
    httpx_mock.add_response(
        url=f"{BASE}/Public",
        text=f'<html><script>var current_committee_id = "{committee_id}";</script></html>',
        is_reusable=True,
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


def test_render_report_covers_ok_and_skipped():
    results = [
        DistrictResult(
            name="Ossining", state="ny", slug="ossining", status="ok",
            committees={"COMM123": ScrapeStats(discovered=5, downloaded=3, skipped_seen=2)},
        ),
        DistrictResult(
            name="Yonkers", state="ny", slug="yonkers", status="skipped",
            error="ProxyError: 403",
        ),
    ]
    md = render_report(results, dry_run=True)
    assert "dry run" in md
    assert "| Ossining | ok | COMM123 | 5 | 3 | 2 | 0 |" in md
    assert "### Needs attention" in md
    assert "Yonkers" in md and "ProxyError" in md


def test_crawl_target_discovers_committee_and_downloads(httpx_mock, tmp_path):
    from herald.scrape.runner import Target

    _mock_district(httpx_mock, committee_id="COMM123")
    manifest = Manifest(tmp_path / "manifest.jsonl")
    store = RawStore(tmp_path / "raw")
    target = Target(district="scarsdale", name="Scarsdale", state="ny", slug="scarsdale")

    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f)
        per_committee = crawl_target(client, target, store=store, manifest=manifest, limit=1)

    # committee id auto-discovered from /Public; one meeting * three files
    assert set(per_committee) == {"COMM123"}
    assert per_committee["COMM123"].downloaded == 3
    assert len(manifest.entries()) == 3


def test_crawl_target_uses_explicit_committee_ids(httpx_mock, tmp_path):
    from herald.scrape.runner import Target

    _mock_district(httpx_mock)
    manifest = Manifest(tmp_path / "manifest.jsonl")
    store = RawStore(tmp_path / "raw")
    # explicit committee id in the target skips discovery
    target = Target(
        district="scarsdale", name="Scarsdale", state="ny", slug="scarsdale",
        committees=["EXPLICIT9"],
    )
    with _fast_fetcher() as f:
        client = BoardDocsClient(state="ny", slug="scarsdale", fetcher=f)
        per_committee = crawl_target(client, target, store=store, manifest=manifest, limit=1)
    assert set(per_committee) == {"EXPLICIT9"}
