"""Tests for the transcribed-contract snapshot and its import.

This snapshot exists for a different reason than the BoardDocs ones. Those are
blocked by an IP range; this one is blocked by the document itself.
`PCTA_Contract_20232027.pdf` is a 56-page scan — `extract_pdf` finds 0
characters and 0 tables — so Port Chester's salary grids never reach
`salary_schedule` however often ingest runs. A committed transcription is the
way in, and it has to leave behind exactly what a crawl would have.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from typer.testing import CliRunner

from herald.extract_schools import CANDIDATE_KEYWORDS
from herald.html_text import extract_html
from herald.scrape.__main__ import app
from herald.scrape.core import Manifest

SNAPSHOT = Path("data/snapshots/pcta-contract-2023-2027.jsonl.gz")
runner = CliRunner()


def _snapshot(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _rec(**over) -> dict:
    rec = {
        "district": "port-chester-rye",
        "doc_type": "contract",
        "title": "PCTA Contract 2023-2027 - Appendix A",
        "source_url": "urn:westchester-schools:ocr:x.pdf#appendix-a",
        "date": None,
        "html": "<h2>Teachers - 2023/24</h2><table><tr><th>Step</th><th>BA</th></tr>"
                "<tr><td>1</td><td>62,731</td></tr></table>",
    }
    rec.update(over)
    return rec


def _import(tmp_path: Path, records: list[dict], *args: str):
    snap = tmp_path / "snap.jsonl.gz"
    _snapshot(snap, records)
    out = tmp_path / "raw"
    res = runner.invoke(app, ["contract-snapshot-import", "--snapshot", str(snap),
                              "--out", str(out), "--no-dry-run", *args])
    assert res.exit_code == 0, res.output
    return out, res


def test_import_leaves_what_a_crawl_would_have(tmp_path):
    out, _ = _import(tmp_path, [_rec()])
    entries = list(Manifest(out / "manifest.jsonl").entries())
    assert len(entries) == 1
    e = entries[0]
    assert e.district == "port-chester-rye"
    assert e.doc_type == "contract"
    assert e.source_url == "urn:westchester-schools:ocr:x.pdf#appendix-a"
    # .html, so ingest dispatches to extract_html and its table-aware chunking
    # rather than to PyMuPDF, which is what could not read the scan to begin with
    assert Path(e.local_path).suffix == ".html"
    assert Path(e.local_path).is_file()


def test_importing_twice_adds_nothing(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _snapshot(snap, [_rec()])
    out = tmp_path / "raw"
    for _ in range(2):
        runner.invoke(app, ["contract-snapshot-import", "--snapshot", str(snap),
                            "--out", str(out), "--no-dry-run"])
    assert len(list(Manifest(out / "manifest.jsonl").entries())) == 1


def test_two_documents_for_one_district_do_not_overwrite_each_other(tmp_path):
    out, _ = _import(tmp_path, [
        _rec(source_url="urn:a#appendix-a", html="<table><tr><td>A</td></tr></table>"),
        _rec(source_url="urn:b#appendix-b", html="<table><tr><td>B</td></tr></table>"),
    ])
    entries = list(Manifest(out / "manifest.jsonl").entries())
    assert len({e.local_path for e in entries}) == 2
    bodies = [Path(e.local_path).read_text(encoding="utf-8") for e in entries]
    assert sorted("A" in b for b in bodies) == [False, True]
    assert sorted("B" in b for b in bodies) == [False, True]


def test_only_filters_by_district(tmp_path):
    out, _ = _import(
        tmp_path,
        [_rec(), _rec(district="ossining", source_url="urn:o#a")],
        "--only", "ossining",
    )
    entries = list(Manifest(out / "manifest.jsonl").entries())
    assert [e.district for e in entries] == ["ossining"]


def test_dry_run_writes_nothing(tmp_path):
    snap = tmp_path / "snap.jsonl.gz"
    _snapshot(snap, [_rec()])
    out = tmp_path / "raw"
    res = runner.invoke(app, ["contract-snapshot-import", "--snapshot", str(snap),
                              "--out", str(out), "--dry-run"])
    assert res.exit_code == 0
    assert not (out / "manifest.jsonl").exists() or not list(
        Manifest(out / "manifest.jsonl").entries()
    )


def test_a_missing_snapshot_is_an_error_not_an_empty_success(tmp_path):
    res = runner.invoke(app, ["contract-snapshot-import",
                              "--snapshot", str(tmp_path / "nope.jsonl.gz"),
                              "--out", str(tmp_path / "raw")])
    assert res.exit_code == 1


# ---- the committed snapshot itself -------------------------------------

def test_the_committed_snapshot_yields_four_candidate_salary_grids(tmp_path):
    """The point of the whole exercise: table chunks herald-extract will read."""
    import re

    assert SNAPSHOT.is_file()
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]
    assert len(recs) == 1 and recs[0]["district"] == "port-chester-rye"

    out, _ = _import(tmp_path, recs)
    html = next(Path(out).rglob("*.html"))
    doc = extract_html(html)

    # one whole-table chunk per school year, 28 steps + header + separator
    assert len(doc.tables) == 4
    kw = re.compile(CANDIDATE_KEYWORDS.replace(r"\y", r"\b"), re.I)
    for t, year in zip(doc.tables, ("2023/24", "2024/25", "2025/26", "2026/27"),
                       strict=True):
        assert year in t.label, t.label
        assert kw.search(t.markdown + " " + t.label), "would not be an extract candidate"
        assert t.markdown.count("\n") + 1 == 30
    # MA+90 must survive as its own lane, not collapse into 'other'
    assert "MA+90" in doc.tables[0].markdown


def test_the_committed_transcription_passes_the_extractor_audit():
    """784 hand-read cells, checked by the invariants that exist for misreads."""
    import importlib.util

    from herald.extract_schools import audit_salary
    from herald.schools_db import SalaryScheduleRow
    from herald.taxonomy import normalize_lane

    spec = importlib.util.spec_from_file_location(
        "build_pcta_snapshot", Path("scripts/build_pcta_snapshot.py")
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rows = []
    for year, grid in mod.GRIDS.items():
        assert len(grid) == 28, year
        for r in grid:
            for lane, salary in zip(mod.LANES, r[1:], strict=True):
                rows.append(("port-chester-rye", SalaryScheduleRow(
                    school_year=year, lane=normalize_lane(lane), lane_raw=lane,
                    step=r[0], years_service=None, is_longevity=False,
                    salary=float(salary), bargaining_unit="teacher",
                )))
    assert len(rows) == 4 * 28 * 7
    assert audit_salary(rows) == []
