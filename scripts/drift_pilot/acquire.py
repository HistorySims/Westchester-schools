"""Find and download one class of annual filing across the peer districts.

Generalizes the safety-plan pilot (docs/DRIFT_PILOT.md) so a second or third
document class costs one invocation rather than another copied script:

    uv run scripts/drift_pilot/acquire.py --doc-class safety-plan
    uv run scripts/drift_pilot/acquire.py --doc-class code-of-conduct

Two sources, as everywhere in this project. The four districts covered by
``data/snapshots/boarddocs-agendas.jsonl.gz`` are read from committed HTML; the
other four are crawled live, because BoardDocs blocks GitHub runner IP ranges
and the snapshot exists for exactly that reason.

**The attachment listings are cached.** Enumerating one district's agendas costs
a request per meeting and the first class took 35 minutes across 1,264
meetings — all of it re-usable, since the listing does not depend on what you
are looking for. With ``agenda_files.json`` populated, a new document class is
a regex over data already on disk. Delete the cache to re-crawl.

Dates are recorded as each hit is found. The first pilot reconstructed them
afterwards by parsing its own log, which worked only because index order
matched log order, and dating by *title* instead put all four of Tarrytowns'
identically-named plans in the earliest year and collapsed them onto one file.
A filing under the wrong year does not go quietly missing — it moves a centroid
years from where it belongs and becomes the strongest signal in the run.
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import sys
import urllib.parse
from dataclasses import dataclass

import typer

from herald.scrape.__main__ import BROWSER_USER_AGENT, _fetcher
from herald.scrape.boarddocs import BoardDocsClient
from herald.scrape.runner import load_targets

app = typer.Typer(add_completion=False)

ROOT = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
SNAPSHOT = pathlib.Path("data/snapshots/boarddocs-agendas.jsonl.gz")
TARGETS = "data/targets/port_chester_peers.json"

#: BoardDocs slug -> district slug, for the four the snapshot does not cover.
CRAWLED = {"oufsd": "ossining", "tufsd": "tarrytowns",
           "elmsford": "elmsford", "wpcsd": "white-plains"}

#: Months an annual filing is adopted or amended in. April, May, November and
#: December are the months no district used for the safety plan, and skipping
#: them cuts the crawl by a third.
ADOPTION_MONTHS = (1, 2, 3, 6, 7, 8, 9, 10)


@dataclass(frozen=True)
class DocClass:
    """What to look for, and what superficially matches but is not it.

    Two kinds of exclusion, because they behave differently:

    ``skip`` names a *different document* — a plain-language summary, a chart
    deck, a code of ethics. Wherever that appears, item title or filename, the
    attachment is the wrong document and is rejected.

    ``skip_item`` names an *agenda process* — a public hearing, a first
    reading, an annual policy review. Those words describe the meeting's
    business, not the file hanging under it, and the real document is very
    often attached to exactly such an item: White Plains files its code of
    conduct under "Public Hearing on the Code of Conduct" every year. Rejecting
    on them outright cost White Plains three of its six years. So they reject
    only when the filename offers no independent evidence of its own.
    """

    name: str
    want: str
    skip: str
    note: str
    skip_item: str = ""


DOC_CLASSES = {
    "safety-plan": DocClass(
        name="safety-plan",
        want=r"(?:district[- ]?wide|dw)\s*(?:school\s*)?safety\s*plan|school\s*safety\s*plan",
        # Policy 8130 is the policy *about* having a plan — governance
        # boilerplate that would sit in the middle of every district's centroid
        # and compress exactly the differences being looked for.
        skip=r"data\s*(?:privacy|security|access)|cyber|parents?\s*bill"
             r"|polic(?:y|ies)\s*#?\s*8130|8130\s*[-–]|\bteams\b",
        note="NY Education Law 2801-a; adopted annually, usually in summer.",
    ),
    "code-of-conduct": DocClass(
        name="code-of-conduct",
        want=r"code\s+of\s+conduct",
        # A code of ETHICS binds board members, not students, and is a
        # different document. The athletic, coaching and extracurricular codes
        # are real documents but narrower ones, and mixing a 4-page athletic
        # code into a peer set of 40-page district codes measures length.
        # Policy 5300 is the policy about having a code.
        # Charts, presentations and hearing slides are *about* the code. They
        # carry its vocabulary, so the regex cannot tell them apart, and under
        # "latest attachment per year wins" a September chart deck beats the
        # June code it summarizes — Greenburgh files both.
        skip=r"ethic|athletic|coach|extra[- ]?curricular|interscholastic"
             r"|transportation|bus\s+conduct|employee|staff\s+code"
             r"|polic(?:y|ies)\s*#?\s*5300|5300\s*[-–]"
             r"|charts?\b|presentation|power\s?point|slides?\b"
             r"|plain\s+language|summary|synopsis|at[- ]a[- ]glance"
             r"|policy\s+exhibit",
        note="NY Education Law 2801; reviewed and adopted annually.",
        skip_item=r"public\s+hearing|first\s+reading|second\s+reading"
                  r"|annual\s+review",
    ),
}


def cache_path() -> pathlib.Path:
    return ROOT / "agenda_files.json"


def load_cache() -> dict[str, list[dict]]:
    p = cache_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_cache(cache: dict[str, list[dict]]) -> None:
    cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache_path().write_text(json.dumps(cache))


def filename_of(url: str) -> str:
    """The real filename out of a BoardDocs attachment URL.

    ``best_title`` prefers the agenda item's title, which is usually the better
    label — but one item can carry several attachments and then they all share
    it. Tarrytowns files the code, a K-5 plain-language summary and a secondary
    plain-language summary under one "Code of Conduct" item, and nothing in the
    item title separates the 40-page document from its 2-page synopsis. The URL
    ends in ``$file/<name>``, so the discriminator was there all along.
    """
    tail = urllib.parse.unquote(url or "").rsplit("$file/", 1)[-1]
    return tail.rsplit("/", 1)[-1]


def matches(title: str, spec: DocClass, *, url: str = "") -> bool:
    """Does this attachment belong to the class?

    The item title and the filename are both consulted, and a skip hit on
    *either* rejects: a summary of the right document is still the wrong
    document, and it is the filename that says so.
    """
    name = filename_of(url)
    if not title and not name:
        return False
    if any(re.search(spec.skip, s, re.I) for s in (title, name) if s):
        return False
    wanted_by_name = bool(name and re.search(spec.want, name, re.I))
    if (spec.skip_item and title and re.search(spec.skip_item, title, re.I)
            and not wanted_by_name):
        return False
    return wanted_by_name or bool(title and re.search(spec.want, title, re.I))


def specificity(title: str, spec: DocClass, url: str = "") -> int:
    """How strongly this attachment claims to *be* the document.

    Used to choose between candidates in one (district, year): a filename
    naming the class outright beats one that only inherited the class from the
    agenda item it hangs under.
    """
    name = filename_of(url)
    return int(bool(name and re.search(spec.want, name, re.I)))


#: An attachment anchor in snapshot HTML. The href and the link text have to be
#: read together, since the date comes from the agenda record around them.
def anchor_re(spec: DocClass) -> re.Pattern[str]:
    core = spec.want.replace(r"\s*", r"\s*").replace(r"\s+", r"\s+")
    return re.compile(
        r"""<a\b[^>]*?href=["']([^"']+)["'][^>]*>([^<>]*?(?:""" + core + r""")[^<>]*)</a>""",
        re.I | re.S,
    )


def from_snapshot(spec: DocClass) -> list[dict]:
    if not SNAPSHOT.exists():
        typer.echo(f"no agenda snapshot at {SNAPSHOT}", err=True)
        return []
    pattern = anchor_re(spec)
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            district, when = record["district"], (record.get("date") or "")[:10]
            for m in pattern.finditer(record["html"]):
                href, text = m.group(1), m.group(2)
                title = re.sub(r"\s*\([\d,]+\s*KB\)$", "",
                               " ".join(text.split())).strip()
                full = href if href.startswith("http") else "https://go.boarddocs.com" + href
                if not matches(title, spec, url=full) or (district, href) in seen:
                    continue
                seen.add((district, href))
                rows.append({"district": district, "date": when,
                             "title": title, "url": full})
    return rows


def from_crawl(spec: DocClass, *, refresh: bool) -> list[dict]:
    cache = {} if refresh else load_cache()
    rows: list[dict] = []
    for target in load_targets(TARGETS):
        if target.slug not in CRAWLED:
            continue
        district = CRAWLED[target.slug]
        cached = [k for k in cache if k.startswith(f"{district}|")]
        if cached:
            typer.echo(f"{district}: {len(cached)} agendas from cache")
            for key in cached:
                when = key.split("|", 2)[1]
                for f in cache[key]:
                    if matches(f["title"], spec, url=f["url"]):
                        rows.append({"district": district, "date": when,
                                     "title": f["title"], "url": f["url"]})
            continue

        with _fetcher(BROWSER_USER_AGENT, 2.0, respect_robots=False, browser=True) as f:
            client = BoardDocsClient(state=target.state, slug=target.slug, fetcher=f)
            try:
                pairs = [(m, c.unique) for c in client.discover_committees()
                         for m in client.list_meetings(c.unique) if m.date]
            except Exception as exc:
                typer.echo(f"{district}: meeting list failed {str(exc)[:70]}", err=True)
                continue
            pairs.sort(key=lambda x: x[0].date, reverse=True)
            wanted = [(m, c) for m, c in pairs if m.date.month in ADOPTION_MONTHS]
            typer.echo(f"{district}: crawling {len(wanted)} of {len(pairs)} meetings")
            failures = 0
            for meeting, committee in wanted:
                try:
                    files = client.get_agenda_files(meeting, committee)
                except Exception:
                    failures += 1
                    continue
                listing = [{"title": fr.best_title or "", "url": fr.url} for fr in files]
                cache[f"{district}|{meeting.date}|{meeting.unique}"] = listing
                for item in listing:
                    if matches(item["title"], spec, url=item["url"]):
                        rows.append({"district": district, "date": str(meeting.date),
                                     "title": item["title"], "url": item["url"]})
                        typer.echo(f"   {meeting.date}  {item['title'][:62]}")
            # A silent failure count is how the first pilot nearly reported a
            # blocked district as a district that files nothing.
            if failures:
                typer.echo(f"{district}: {failures} agendas could not be read", err=True)
            save_cache(cache)
    return rows


def school_year(title: str, when: str) -> str:
    """"2025-26", from the title where it says so, else from the meeting date.

    The paired number has to actually be the following year. Districts put
    revision dates in filenames — "Code of Conduct (revised 2024-06-21)" — and
    reading the first two numbers as a span filed that one under school year
    "2024-06", which belongs to no peer set and silently drops the district
    from the year it was really adopted in.
    """
    m = re.search(r"(20\d\d)\s*[-–/ ]\s*(\d{2,4})\b", title or "")
    if m and 2010 < int(m.group(1)) < 2030:
        start, paired = int(m.group(1)), int(m.group(2))
        if paired in ((start + 1) % 100, start + 1):
            return f"{start}-{str(start + 1)[-2:]}"
    m = re.search(r"\b(20\d\d)\b", title or "")
    if m:
        year = int(m.group(1))
        return f"{year}-{str(year + 1)[-2:]}"
    year, month = int(when[:4]), int(when[5:7])
    start = year if month >= 6 else year - 1
    return f"{start}-{str(start + 1)[-2:]}"


@app.command()
def main(
    doc_class: str = typer.Option(..., "--doc-class", help=f"One of {sorted(DOC_CLASSES)}"),
    refresh: bool = typer.Option(False, help="Re-crawl instead of using the cache."),
    download: bool = typer.Option(True, help="Fetch the files, not just list them."),
) -> None:
    if doc_class not in DOC_CLASSES:
        typer.echo(f"unknown class {doc_class!r}; have {sorted(DOC_CLASSES)}", err=True)
        raise typer.Exit(1)
    spec = DOC_CLASSES[doc_class]
    out = ROOT / doc_class
    out.mkdir(parents=True, exist_ok=True)

    rows = from_crawl(spec, refresh=refresh) + from_snapshot(spec)
    for r in rows:
        r["school_year"] = school_year(r["title"], r["date"])
        r["filename"] = filename_of(r["url"])
        r["specificity"] = specificity(r["title"], spec, r["url"])
    rows.sort(key=lambda r: (r["district"], r["date"]))
    (out / "index.json").write_text(json.dumps(rows, indent=1))
    typer.echo(f"\n{len(rows)} candidate filings of class {doc_class}")

    # One per (district, year). A filename that names the class outright wins
    # over one that only inherited it from the agenda item, and within that the
    # latest attachment wins — the adopted version rather than a draft
    # circulated earlier in the same cycle.
    best: dict[tuple[str, str], dict] = {}
    for r in sorted(rows, key=lambda r: (r["specificity"], r["date"])):
        best[(r["district"], r["school_year"])] = r
    typer.echo(f"{len(best)} (district, year) filings\n")

    manifest: list[dict] = []
    for district in sorted({d for d, _ in best}):
        with _fetcher(BROWSER_USER_AGENT, 1.5, respect_robots=False, browser=True) as f:
            for (d, year), r in sorted(best.items()):
                if d != district:
                    continue
                ext = ".doc" if r["title"].lower().endswith(".doc") else ".pdf"
                path = out / f"{d}__{year}{ext}"
                if download and (not path.exists() or path.stat().st_size < 2000):
                    try:
                        body = f.get(r["url"]).content
                    except Exception as exc:
                        typer.echo(f"   !! {d} {year}: {str(exc)[:60]}", err=True)
                        continue
                    if len(body) < 2000:
                        typer.echo(f"   !! {d} {year}: {len(body)} bytes", err=True)
                        continue
                    path.write_bytes(body)
                if not path.exists():
                    continue
                manifest.append({
                    "district": d, "school_year": year, "meeting_date": r["date"],
                    "title": r["title"], "url": r["url"], "path": str(path),
                    "bytes": path.stat().st_size,
                })
                typer.echo(f"{d:<19}{year}  {path.stat().st_size / 1024:>7.0f} KB  "
                           f"{r['date']}  {r['title'][:42]}")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    by_year: dict[str, list[str]] = {}
    for m in manifest:
        by_year.setdefault(m["school_year"], []).append(m["district"])
    typer.echo(f"\n{len(manifest)} files on disk\n\nschool year   districts")
    for year in sorted(by_year):
        typer.echo(f"  {year:<11} {len(by_year[year])}  {' '.join(sorted(by_year[year]))}")


if __name__ == "__main__":
    sys.exit(app())
