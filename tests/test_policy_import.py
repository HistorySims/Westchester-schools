"""Tests for importing the committed policy snapshot.

The snapshot exists because BoardDocs caps an anonymous datacenter IP at
roughly twenty policy fetches before answering 403 for the whole host — 19
policies a run against a 1,077-policy corpus. The manuals were collected from
an unblocked connection and committed; this is the path that puts them in the
raw store, and it must leave behind exactly what a scrape would have.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from typer.testing import CliRunner

from herald.scrape.__main__ import app
from herald.scrape.core import Manifest

SNAPSHOT = Path("data/snapshots/boarddocs-policies.jsonl.gz")
runner = CliRunner()


def _write_snapshot(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _rec(url: str, *, district="tarrytowns", title="5100 ATTENDANCE", body="text") -> dict:
    return {
        "district": district,
        "title": title,
        "source_url": url,
        "fetched_at": "2026-08-21T00:00:00Z",
        "html": f"<div id='forcopy'><p>{body}</p></div>",
    }


def test_import_writes_the_raw_store_and_manifest_a_scrape_would_have(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _write_snapshot(snap, [
        _rec("https://go.boarddocs.com/ny/tufsd/Board.nsf/goto?open&id=A"),
        _rec("https://go.boarddocs.com/ny/wpcsd/Board.nsf/goto?open&id=B",
             district="white-plains", title="1741 HOME-SCHOOLED STUDENTS"),
    ])
    out = tmp_path / "raw"
    res = runner.invoke(app, ["policy-import", "--snapshot", str(snap), "--out", str(out)])
    assert res.exit_code == 0, res.output

    entries = Manifest(out / "manifest.jsonl").entries()
    assert len(entries) == 2
    by_district = {e.district: e for e in entries}
    assert set(by_district) == {"tarrytowns", "white-plains"}
    e = by_district["tarrytowns"]
    # the permalink is the whole point of the snapshot: answers cite it
    assert e.source_url.endswith("goto?open&id=A")
    assert e.doc_type.value == "policy"
    assert Path(e.local_path).is_file()
    assert Path(e.local_path).suffix == ".html"
    assert "5100" in Path(e.local_path).read_text(encoding="utf-8") or e.title == "5100 ATTENDANCE"


def test_importing_twice_adds_nothing(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _write_snapshot(snap, [_rec("https://d.test/goto?open&id=A")])
    out = tmp_path / "raw"
    args = ["policy-import", "--snapshot", str(snap), "--out", str(out)]
    runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert second.exit_code == 0
    assert "skipped 1" in second.output
    assert len(Manifest(out / "manifest.jsonl").entries()) == 1


def test_two_policies_sharing_a_body_both_survive(tmp_path):
    # White Plains 1741 is byte-identical to another policy. Deduping on
    # content alone dropped one of them, and its number and permalink with it.
    snap = tmp_path / "snap.jsonl.gz"
    _write_snapshot(snap, [
        _rec("https://d.test/goto?open&id=A", title="1741 HOME-SCHOOLED STUDENTS"),
        _rec("https://d.test/goto?open&id=B", title="4321 TWIN WITH THE SAME TEXT"),
    ])
    out = tmp_path / "raw"
    runner.invoke(app, ["policy-import", "--snapshot", str(snap), "--out", str(out)])
    assert len(Manifest(out / "manifest.jsonl").entries()) == 2


def test_only_filters_to_one_district(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _write_snapshot(snap, [
        _rec("https://d.test/goto?open&id=A"),
        _rec("https://d.test/goto?open&id=B", district="mount-vernon"),
    ])
    out = tmp_path / "raw"
    runner.invoke(app, ["policy-import", "--snapshot", str(snap),
                        "--out", str(out), "--only", "mount-vernon"])
    entries = Manifest(out / "manifest.jsonl").entries()
    assert [e.district for e in entries] == ["mount-vernon"]


def test_dry_run_writes_nothing(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _write_snapshot(snap, [_rec("https://d.test/goto?open&id=A")])
    out = tmp_path / "raw"
    res = runner.invoke(app, ["policy-import", "--snapshot", str(snap),
                              "--out", str(out), "--dry-run"])
    assert res.exit_code == 0
    assert "would import 1" in res.output
    assert not (out / "manifest.jsonl").exists()


def test_a_missing_snapshot_is_an_error_not_an_empty_success(tmp_path):
    res = runner.invoke(app, ["policy-import", "--snapshot", str(tmp_path / "nope.gz")])
    assert res.exit_code == 1


def test_the_committed_snapshot_holds_all_four_boarddocs_districts():
    # The four peer districts with no external policy portal. If this file
    # shrinks, the corpus silently loses policies.
    assert SNAPSHOT.is_file()
    counts: dict[str, int] = {}
    urls: set[str] = set()
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            counts[rec["district"]] = counts.get(rec["district"], 0) + 1
            assert rec["html"].strip(), rec["title"]
            assert "boarddocs.com" in rec["source_url"]
            urls.add(rec["source_url"])
    assert set(counts) == {
        "tarrytowns", "white-plains", "mount-vernon", "greenburgh-central",
    }
    assert sum(counts.values()) == 1071
    assert len(urls) == 1071          # every policy keeps its own permalink
