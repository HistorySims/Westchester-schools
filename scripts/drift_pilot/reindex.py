"""Rebuild the plan index with a real meeting date on every row.

index.json carries (title, url) only, and dating by title fails exactly where
it matters most: Tarrytowns names all four of its plans "District Wide School
Safety Plan" and Greenburgh three of its "District-Wide School Safety Plan.pdf",
so matching on text put every one of them in the earliest year and collapsed
them onto a single file. A plan filed under the wrong year does not merely go
missing — it moves a centroid years away from where it belongs and shows up as
the strongest "moved alone" signal in the run.

Dates come from two places, each exact:

* **Crawled districts** — plans.py printed ``   <date>  <title>`` as it
  appended to the index, so the Nth logged line for a district is the Nth row
  of its index. Positional, not textual, so repeated titles stay distinct.
* **Snapshot districts** — the committed agenda HTML is keyed by meeting date
  already, so the anchor and its date are read together.
"""
from __future__ import annotations

import os
import gzip
import json
import pathlib
import re
import sys

SCRATCH = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
PLANS = SCRATCH / "plans"
LOG = SCRATCH / "probe" / "plans.log"

#: plans.py prints the district header, then one indented line per hit.
HEADER = re.compile(r"^(\S[\w-]*): scanning \d+ meetings")
HIT = re.compile(r"^\s{3}(\d{4}-\d{2}-\d{2})\s{2}(.+?)\s*$")

#: An attachment anchor in the snapshot HTML, href and text together.
ANCHOR = re.compile(
    r"""<a\b[^>]*?href=["']([^"']+)["'][^>]*>([^<>]*safety\s*plan[^<>]*)</a>""",
    re.I | re.S,
)

WANT = re.compile(
    r"(?:district[- ]?wide|dw)\s*(?:school\s*)?safety\s*plan|school\s*safety\s*plan", re.I
)
SKIP = re.compile(r"data\s*(?:privacy|security|access)|cyber|parents?\s*bill", re.I)
#: The policy *about* having a plan — governance boilerplate, not a plan.
NOT_A_PLAN = re.compile(r"polic(?:y|ies)\s*#?\s*8130|8130\s*[-–]|\bteams\b", re.I)

CRAWLED = {"ossining", "tarrytowns", "elmsford", "white-plains"}


def want(title: str) -> bool:
    return bool(title) and bool(WANT.search(title)) and not SKIP.search(title)


def log_dates() -> dict[str, list[tuple[str, str]]]:
    """district -> [(date, logged title)] in the order the crawl found them."""
    out: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for line in LOG.read_text(errors="replace").splitlines():
        h = HEADER.match(line)
        if h:
            current = h.group(1)
            out.setdefault(current, [])
            continue
        m = HIT.match(line)
        if m and current:
            out[current].append((m.group(1), m.group(2)))
    return out


def snapshot_rows() -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with gzip.open("data/snapshots/boarddocs-agendas.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            when, district = (r.get("date") or "")[:10], r["district"]
            for m in ANCHOR.finditer(r["html"]):
                href, text = m.group(1), m.group(2)
                title = re.sub(r"\s*\([\d,]+\s*KB\)$", "", " ".join(text.split())).strip()
                if not want(title):
                    continue
                url = href if href.startswith("http") else "https://go.boarddocs.com" + href
                if (district, url) in seen:
                    continue
                seen.add((district, url))
                rows.append({"district": district, "date": when, "title": title, "url": url})
    return rows


def main() -> int:
    idx = json.loads((PLANS / "index.json").read_text())
    logged = log_dates()
    rows: list[dict] = []

    for district, items in idx.items():
        if district not in CRAWLED:
            continue
        dates = logged.get(district, [])
        if len(dates) != len(items):
            print(f"!! {district}: {len(items)} index rows vs {len(dates)} logged — "
                  "cannot align positionally")
            continue
        for (when, logged_title), (title, url) in zip(dates, items):
            # The log truncates at 66 chars; a prefix mismatch means the
            # positional assumption is broken and the dates cannot be trusted.
            if not title.startswith(logged_title[:40]):
                print(f"!! {district}: log/index out of step at {when} — "
                      f"{logged_title[:40]!r} vs {title[:40]!r}")
                return 1
            rows.append({"district": district, "date": when, "title": title, "url": url})

    rows += snapshot_rows()
    rows = [r for r in rows if not NOT_A_PLAN.search(r["title"])]
    rows.sort(key=lambda r: (r["district"], r["date"]))

    (PLANS / "dated_index.json").write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} dated plan rows\n")
    for d in sorted({r['district'] for r in rows}):
        mine = [r for r in rows if r["district"] == d]
        print(f"{d}: {len(mine)}")
        for r in mine:
            print(f"   {r['date']}  {r['title'][:62]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
