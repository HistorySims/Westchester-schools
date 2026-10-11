"""Download the plans named in dated_index.json, one file per (district, year).

The year comes from the title when the district puts one there and from the
adopting meeting otherwise. Several districts attach the same plan to two or
three consecutive meetings — Greenburgh filed "District-Wide School Safety
Plan.pdf" at three meetings six weeks apart — so the last attachment of a given
(district, year) wins and the earlier duplicates are skipped rather than
averaged into the centroid twice.
"""
from __future__ import annotations

import os
import json
import pathlib
import re
import sys
from datetime import date

from herald.scrape.__main__ import BROWSER_USER_AGENT, _fetcher

SCRATCH = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
PLANS = SCRATCH / "plans"


def school_year(title: str, when: date) -> str:
    """"2025-26", from the title where it says so, else from the meeting."""
    m = re.search(r"(20\d\d)\s*[-–/ ]\s*(\d{2,4})\b", title or "")
    if m and 2010 < int(m.group(1)) < 2030:
        return f"{m.group(1)}-{m.group(2)[-2:]}"
    m = re.search(r"\b(20\d\d)\b", title or "")
    if m:
        y = int(m.group(1))
        return f"{y}-{str(y + 1)[-2:]}"
    # No year in the title: a plan adopted in summer or autumn covers the year
    # starting; one amended in January covers the year already under way.
    y = when.year if when.month >= 6 else when.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def main() -> int:
    rows = json.loads((PLANS / "dated_index.json").read_text())
    for r in rows:
        r["when"] = date.fromisoformat(r["date"])
        r["school_year"] = school_year(r["title"], r["when"])

    # One per (district, year): the latest attachment, which is the adopted
    # version rather than the first draft circulated.
    best: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda r: r["when"]):
        best[(r["district"], r["school_year"])] = r
    print(f"{len(rows)} rows -> {len(best)} (district, year) plans\n")

    manifest: list[dict] = []
    for district in sorted({d for d, _ in best}):
        with _fetcher(BROWSER_USER_AGENT, 1.5, respect_robots=False, browser=True) as f:
            for (d, yr), r in sorted(best.items()):
                if d != district:
                    continue
                ext = ".doc" if r["title"].lower().endswith(".doc") else ".pdf"
                path = PLANS / f"{d}__{yr}{ext}"
                if not path.exists() or path.stat().st_size < 2000:
                    try:
                        body = f.get(r["url"]).content
                    except Exception as exc:
                        print(f"   !! {yr} {r['title'][:46]}: {str(exc)[:60]}")
                        continue
                    if len(body) < 2000:
                        print(f"   !! {yr} {r['title'][:46]}: {len(body)} bytes")
                        continue
                    path.write_bytes(body)
                manifest.append({
                    "district": d, "school_year": yr, "meeting_date": r["date"],
                    "title": r["title"], "url": r["url"], "path": str(path),
                    "bytes": path.stat().st_size,
                })
                print(f"{d:<18} {yr}  {path.stat().st_size / 1024:>7.0f} KB  "
                      f"{r['date']}  {r['title'][:44]}")

    (PLANS / "manifest.json").write_text(json.dumps(manifest, indent=1))
    by_year: dict[str, list[str]] = {}
    for m in manifest:
        by_year.setdefault(m["school_year"], []).append(m["district"])
    print(f"\n{len(manifest)} plans on disk\n\nschool year   districts")
    for yr in sorted(by_year):
        print(f"  {yr:<11} {len(by_year[yr])}  {' '.join(sorted(by_year[yr]))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
