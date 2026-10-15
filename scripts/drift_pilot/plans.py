"""Fetch district-wide safety plans across all eight districts.

Snapshot districts give their attachment links straight from committed HTML;
the other four are crawled.
"""
import os
import gzip, json, pathlib, re
from bs4 import BeautifulSoup
from herald.scrape.__main__ import BROWSER_USER_AGENT, _fetcher
from herald.scrape.boarddocs import BoardDocsClient
from herald.scrape.core import attr
from herald.scrape.runner import load_targets

OUT = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot")) / "plans"
OUT.mkdir(parents=True, exist_ok=True)

# A district-wide safety plan, not a data-security plan or a building committee.
WANT = re.compile(r"(?:district[- ]?wide|dw)\s*(?:school\s*)?safety\s*plan"
                  r"|school\s*safety\s*plan", re.I)
SKIP = re.compile(r"data\s*(?:privacy|security|access)|cyber|parents?\s*bill", re.I)

def want(title: str) -> bool:
    return bool(title) and bool(WANT.search(title)) and not SKIP.search(title)

found: dict[str, list[tuple[str, str]]] = {}      # district -> [(title, url)]

# --- the four snapshot districts -------------------------------------------
snap = [json.loads(l) for l in gzip.open("data/snapshots/boarddocs-agendas.jsonl.gz","rt",encoding="utf-8")]
for r in snap:
    soup = BeautifulSoup(r["html"], "html.parser")
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if not want(t):
            continue
        href = attr(a, "href")
        url = href if href.startswith("http") else "https://go.boarddocs.com" + href
        found.setdefault(r["district"], [])
        if url not in {u for _, u in found[r["district"]]}:
            found[r["district"]].append((re.sub(r"\s*\([\d,]+\s*KB\)$", "", t).strip(), url))

# --- the four the snapshot does not cover ----------------------------------
REST = {"oufsd": "ossining", "tufsd": "tarrytowns",
        "elmsford": "elmsford", "wpcsd": "white-plains"}
for t in load_targets("data/targets/port_chester_peers.json"):
    if t.slug not in REST:
        continue
    with _fetcher(BROWSER_USER_AGENT, 2.0, respect_robots=False, browser=True) as f:
        client = BoardDocsClient(state=t.state, slug=t.slug, fetcher=f)
        try:
            pairs = [(m, c.unique) for c in client.discover_committees()
                     for m in client.list_meetings(c.unique) if m.date]
        except Exception as exc:
            print(f"{t.district}: meeting list failed {str(exc)[:70]}"); continue
        pairs.sort(key=lambda x: x[0].date, reverse=True)
        print(f"{t.district}: scanning {len(pairs)} meetings for safety plans", flush=True)
        for m, cid in pairs:
            # Only summer/early-autumn meetings adopt the annual plan.
            if m.date.month not in (6, 7, 8, 9, 10, 1, 2, 3):
                continue
            try:
                files = client.get_agenda_files(m, cid)
            except Exception:
                continue
            for fr in files:
                if want(fr.best_title or ""):
                    found.setdefault(t.district, [])
                    if fr.url not in {u for _, u in found[t.district]}:
                        found[t.district].append((fr.best_title, fr.url))
                        print(f"   {m.date}  {fr.best_title[:66]}", flush=True)

print("\n=== candidates ===")
for d in sorted(found):
    print(f"{d}: {len(found[d])}")
    for t, _ in found[d]:
        print(f"    {t[:76]}")
json.dump({d: found[d] for d in found}, open(OUT/"index.json","w"), indent=1)
