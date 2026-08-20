"""CLI for the scrape layer:  ``python -m herald.scrape --help``

Deliberately independent of ``herald.cli`` (which is still bannered for the
ingest rewrite) so the scraper is runnable today. Typical first session::

    # 1. find the committee ids for your district
    python -m herald.scrape committees --state ny --slug scarsdale

    # 2. eyeball what a committee's meetings look like
    python -m herald.scrape meetings --state ny --slug scarsdale --committee <id>

    # 3. see what WOULD download, then do it
    python -m herald.scrape fetch --state ny --slug scarsdale --committee <id> \\
        --district scarsdale --since 2023-01-01 --dry-run
    python -m herald.scrape fetch --state ny --slug scarsdale --committee <id> \\
        --district scarsdale --since 2023-01-01
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from herald.scrape.boarddocs import (
    BoardDocsClient,
    CommitteeNotFound,
    analyze_public_html,
    iter_documents,
)
from herald.scrape.core import (
    BROWSER_HEADERS,
    BROWSER_USER_AGENT,
    DEFAULT_USER_AGENT,
    Fetcher,
    Manifest,
    RawStore,
)
from herald.scrape.runner import (
    DistrictResult,
    crawl_target,
    download_docs,
    load_targets,
    render_report,
)
from herald.scrape.site import crawl_site, docs_from_seed

app = typer.Typer(help="Scrape district sources into raw files + a manifest.", no_args_is_help=True)
console = Console()

CONTACT_EMAIL = "timhartnett29@gmail.com"


def _fetcher(
    user_agent: str,
    min_interval: float,
    respect_robots: bool = True,
    browser: bool = True,
) -> Fetcher:
    if browser:
        # Present as a browser (some hosts 403 non-browser clients) while
        # keeping an honest contact via the From header + polite rate limit.
        return Fetcher(
            user_agent=BROWSER_USER_AGENT,
            headers={**BROWSER_HEADERS, "From": CONTACT_EMAIL},
            min_request_interval=min_interval,
            respect_robots=respect_robots,
        )
    return Fetcher(
        user_agent=user_agent,
        min_request_interval=min_interval,
        respect_robots=respect_robots,
    )


@app.command()
def committee(
    state: str = typer.Option(..., help="BoardDocs state slug, e.g. 'ny'."),
    slug: str = typer.Option(..., help="District slug in the BoardDocs URL."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT, help="Identifying User-Agent."),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Discover a district's committee id(s) from its /Public page."""
    with _fetcher(user_agent, min_interval) as fetcher:
        client = BoardDocsClient(state=state, slug=slug, fetcher=fetcher)
        committees = client.discover_committees()
    if committees:
        table = Table("committeeid", "name", title=f"{state}/{slug} committees")
        for c in committees:
            table.add_row(c.unique, c.name)
        console.print(table)
    else:
        console.print(f"[yellow]no committee found on {client.public_url}[/yellow]")


@app.command()
def meetings(
    state: str = typer.Option(...),
    slug: str = typer.Option(...),
    committee: str = typer.Option(..., help="Committee 'unique' id from `committees`."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """List meetings for one committee."""
    with _fetcher(user_agent, min_interval) as fetcher:
        client = BoardDocsClient(state=state, slug=slug, fetcher=fetcher)
        rows = client.list_meetings(committee)
    table = Table("date", "name", "unique", title=f"{state}/{slug} meetings")
    for m in rows:
        table.add_row(str(m.date or "?"), m.name, m.unique)
    console.print(table)


@app.command()
def fetch(
    state: str = typer.Option(...),
    slug: str = typer.Option(...),
    committee: str = typer.Option(..., help="Committee 'unique' id to crawl."),
    district: str = typer.Option(..., help="District name to tag documents with."),
    committee_name: str | None = typer.Option(None, help="Human name for the committee."),
    since: str | None = typer.Option(
        None, help="Only meetings on/after this date (YYYY-MM-DD)."
    ),
    limit: int | None = typer.Option(None, help="Cap number of meetings walked."),
    out: str = typer.Option("data/raw", help="Root dir for downloaded files."),
    manifest_path: str | None = typer.Option(
        None, help="Manifest JSONL path (default: <out>/manifest.jsonl)."
    ),
    dry_run: bool = typer.Option(False, help="Discover + list only; download nothing."),
    ignore_robots: bool = typer.Option(
        False, help="Bypass robots.txt (only for public records you're entitled to)."
    ),
    browser: bool = typer.Option(
        True, help="Present as a browser + prime a session (needed past BoardDocs' bot filter)."
    ),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Discover a committee's attachments and download them."""
    since_date = date.fromisoformat(since) if since else None
    out_dir = Path(out)
    mpath = Path(manifest_path) if manifest_path else out_dir / "manifest.jsonl"
    store = RawStore(out_dir)
    manifest = Manifest(mpath)

    with _fetcher(
        user_agent, min_interval, respect_robots=not ignore_robots, browser=browser
    ) as fetcher:
        client = BoardDocsClient(state=state, slug=slug, fetcher=fetcher, prime_session=browser)
        docs = iter_documents(
            client,
            district=district,
            committee=committee,
            committee_name=committee_name,
            since=since_date,
            limit=limit,
        )
        stats = download_docs(
            docs, fetcher=fetcher, store=store, manifest=manifest, dry_run=dry_run
        )

    verb = "Would download" if dry_run else "Downloaded"
    console.print(
        f"[bold]{verb}[/bold]: {stats.downloaded} new "
        f"(discovered {stats.discovered}, skipped {stats.skipped_seen} seen, "
        f"{stats.skipped_dup_content} dup-content, {stats.failed} failed)"
    )
    if stats.by_type:
        console.print("by type: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.by_type.items())))
    if not dry_run:
        console.print(f"manifest: {mpath}")


@app.command()
def crawl(
    targets: str = typer.Option(..., help="Path to a targets JSON file."),
    only: str | None = typer.Option(
        None, help="Crawl only target(s) whose slug or district matches (comma-separated)."
    ),
    since: str | None = typer.Option(None, help="Only meetings on/after this date (YYYY-MM-DD)."),
    limit: int | None = typer.Option(None, help="Cap meetings walked per committee."),
    out: str = typer.Option("data/raw", help="Root dir for downloaded files."),
    report: str | None = typer.Option(None, help="Write a markdown summary to this path."),
    dry_run: bool = typer.Option(False, help="Discover + list only; download nothing."),
    ignore_robots: bool = typer.Option(
        False, help="Bypass robots.txt (only for public records you're entitled to)."
    ),
    browser: bool = typer.Option(
        True, help="Present as a browser + prime a session (needed past BoardDocs' bot filter)."
    ),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Batch-crawl every district in a targets file (e.g. Port Chester peers).

    Each district's slug is confirmed as it goes: if BoardDocs rejects it, the
    district is reported as skipped and the crawl moves on.
    """
    since_date = date.fromisoformat(since) if since else None
    out_dir = Path(out)
    manifest = Manifest(out_dir / "manifest.jsonl")
    target_list = load_targets(targets)
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        target_list = [t for t in target_list if t.slug in wanted or t.district in wanted]
        if not target_list:
            console.print(f"[red]no targets matched --only {only}[/red]")
            raise typer.Exit(1)
    results: list[DistrictResult] = []

    for t in target_list:
        console.rule(f"{t.name}  ({t.state}/{t.slug})")
        try:
            with _fetcher(
                user_agent, min_interval, respect_robots=not ignore_robots, browser=browser
            ) as fetcher:
                client = BoardDocsClient(
                    state=t.state, slug=t.slug, fetcher=fetcher, prime_session=browser
                )
                per_committee = crawl_target(
                    client,
                    t,
                    store=RawStore(out_dir),
                    manifest=manifest,
                    since=since_date,
                    limit=limit,
                    dry_run=dry_run,
                )
        except Exception as exc:  # bad slug / not BoardDocs / no committee id / network
            err = f"{type(exc).__name__}: {exc}"
            console.print(f"  [red]skipped[/red]: {err}")
            # Self-diagnose a committee-discovery miss: dump the /Public page
            # into the artifact and surface status + committee hints, so the
            # next run tells us why the id wasn't found (no separate probe).
            cl = locals().get("client")
            if isinstance(exc, CommitteeNotFound) and cl is not None:
                ddir = out_dir / "diagnostics"
                ddir.mkdir(parents=True, exist_ok=True)
                (ddir / f"{t.slug}-Public.html").write_text(cl.public_html, encoding="utf-8")
                info = analyze_public_html(cl.public_html, status=cl.public_status or 0)
                err += (
                    f" | /Public status={cl.public_status} err={cl.public_error} "
                    f"bytes={info.length} hints={info.committee_hints[:3]}"
                )
            results.append(
                DistrictResult(
                    name=t.name, state=t.state, slug=t.slug, status="skipped", error=err,
                )
            )
            continue
        results.append(
            DistrictResult(
                name=t.name, state=t.state, slug=t.slug, status="ok",
                committees=per_committee,
            )
        )
        for cid, s in per_committee.items():
            verb = "would download" if dry_run else "downloaded"
            console.print(
                f"  committee {cid}: {verb} {s.downloaded} "
                f"(discovered {s.discovered}, skipped {s.skipped_seen}, failed {s.failed})"
            )

    if report:
        Path(report).write_text(render_report(results, dry_run=dry_run), encoding="utf-8")
        console.print(f"\nreport: {report}")
    if not dry_run:
        console.print(f"manifest: {out_dir / 'manifest.jsonl'}")


@app.command()
def probe(
    targets: str = typer.Option("data/targets/port_chester_peers.json", help="Targets JSON."),
    out: str = typer.Option("data/probe", help="Where to dump captured HTML/JS."),
    scripts: int = typer.Option(5, help="How many same-origin scripts to save (anchor only)."),
    browser: bool = typer.Option(True),
    ignore_robots: bool = typer.Option(True),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Capture the real BoardDocs public page + endpoint behavior.

    For each target, saves the public-page HTML. For the first (anchor)
    target it also saves its same-origin scripts and status-checks candidate
    AJAX endpoints — enough to reverse-engineer the real API. Everything lands
    under --out so the workflow can upload it as an artifact.
    """
    import httpx

    from herald.scrape.boarddocs import analyze_public_html

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ["## BoardDocs probe", ""]
    target_list = load_targets(targets)

    for i, t in enumerate(target_list):
        base = f"https://go.boarddocs.com/{t.state}/{t.slug}/Board.nsf"
        with _fetcher(
            user_agent, min_interval, respect_robots=not ignore_robots, browser=browser
        ) as f:
            console.rule(f"{t.name} ({t.state}/{t.slug})")
            try:
                resp = f.get(f"{base}/Public")
            except Exception as exc:
                lines.append(f"### {t.name} (`{t.state}/{t.slug}`) — ERROR: {exc}")
                console.print(f"  [red]{exc}[/red]")
                continue

            html = resp.text
            (out_dir / f"{t.slug}-Public.html").write_text(html, encoding="utf-8")
            info = analyze_public_html(html, status=resp.status_code)
            lines.append(
                f"### {t.name} (`{t.state}/{t.slug}`) — HTTP {info.status}, {info.length} bytes"
            )
            lines.append(f"- scripts: `{info.script_srcs[:8]}`")
            lines.append(f"- committee hints: `{info.committee_hints[:6]}`")

            if i == 0:  # deep-probe the anchor only
                saved = 0
                for src in info.script_srcs:
                    url = src if src.startswith("http") else f"https://go.boarddocs.com{src}"
                    if "go.boarddocs.com" not in url or saved >= scripts:
                        continue
                    try:
                        js = f.get(url)
                        name = url.rsplit("/", 1)[-1].split("?")[0] or f"script{saved}.js"
                        (out_dir / f"{t.slug}-{name}").write_text(js.text, encoding="utf-8")
                        saved += 1
                    except Exception as exc:
                        lines.append(f"  - script fetch failed {url}: {exc}")
                lines.append(f"- saved {saved} script file(s) for endpoint discovery")

                candidates = [
                    "BD-GetMeetingsList", "BD-GetAgenda", "BD-GetCommittees",
                    "BD-GetCommitteeList", "BD-GetActiveCommittees", "BD-GetMeeting",
                    "BD-GetItem", "XX-GetMeetingsList",
                ]
                lines.append("- endpoint status scan:")
                for ep in candidates:
                    url = f"{base}/{ep}?open"
                    try:
                        r = f.post(url, data={}, headers={"X-Requested-With": "XMLHttpRequest"})
                        code = r.status_code
                    except httpx.HTTPStatusError as exc:
                        code = exc.response.status_code
                    except Exception as exc:  # record any transport error, keep scanning
                        code = f"ERR {type(exc).__name__}"
                    lines.append(f"    - `{ep}` -> {code}")

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"\nwrote {out_dir}/summary.md and captured HTML/JS under {out_dir}/")


@app.command()
def site(
    url: str = typer.Option(..., help="District website base URL, e.g. https://www.tufsd.org"),
    district: str = typer.Option(..., help="District tag for the manifest."),
    out: str = typer.Option("data/raw", help="Root dir for downloaded files."),
    report: str | None = typer.Option(None, help="Write a markdown summary to this path."),
    max_pages: int = typer.Option(80, help="Max site pages to walk."),
    all_pdfs: bool = typer.Option(False, help="Keep every PDF, not just target doc types."),
    dry_run: bool = typer.Option(False, help="Discover + list only; download nothing."),
    ignore_robots: bool = typer.Option(False, help="Bypass robots.txt (public records)."),
    browser: bool = typer.Option(True, help="Present as a browser."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Crawl a district website for PDF documents (handbooks, contracts, …)."""
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir / "manifest.jsonl")
    with _fetcher(user_agent, min_interval, respect_robots=not ignore_robots, browser=browser) as f:
        docs = list(crawl_site(
            f, base_url=url, district=district, max_pages=max_pages, target_only=not all_pdfs
        ))
        # Always record what was discovered (diagnostic; works even on dry runs).
        disc = out_dir / f"discovered-{district}.jsonl"
        with disc.open("w", encoding="utf-8") as fh:
            for d in docs:
                fh.write(json.dumps(
                    {"doc_type": str(d.doc_type), "title": d.title, "url": d.source_url}
                ) + "\n")
        stats = download_docs(
            docs, fetcher=f, store=RawStore(out_dir), manifest=manifest, dry_run=dry_run
        )

    by_type = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_type.items())) or "none"
    verb = "would download" if dry_run else "downloaded"
    console.print(
        f"[bold]{district}[/bold]: {verb} {stats.downloaded} "
        f"(discovered {stats.discovered}, skipped {stats.skipped_seen}, failed {stats.failed})"
    )
    console.print(f"by type: {by_type}")
    if report:
        lines = [
            f"## Site crawl — {district}", "",
            f"- source: {url}",
            f"- {verb}: **{stats.downloaded}** (discovered {stats.discovered}, "
            f"failed {stats.failed})",
            f"- by type: {by_type}",
        ]
        Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.command()
def contracts(
    sources: str = typer.Option(
        "data/targets/cba_sources.json", help="Seeds JSON (per-district CBA source URLs)."
    ),
    only: str | None = typer.Option(
        None, help="Only these district slug(s), comma-separated."
    ),
    out: str = typer.Option("data/raw", help="Root dir for downloaded files."),
    report: str | None = typer.Option(None, help="Write a markdown summary to this path."),
    max_pages: int = typer.Option(60, help="Max pages to walk per seed site."),
    all_pdfs: bool = typer.Option(
        False, help="Keep every PDF from the seed sites, not just contract-type."
    ),
    dry_run: bool = typer.Option(False, help="Discover + list only; download nothing."),
    ignore_robots: bool = typer.Option(
        True, help="Bypass robots.txt (public records; union sites)."
    ),
    browser: bool = typer.Option(True, help="Present as a browser."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Crawl teacher-union sites + district HR pages for CBAs / salary schedules.

    Teacher salary schedules mostly live OUTSIDE the board-docs corpus. This
    reads ``data/targets/cba_sources.json`` (per district: HR/union page seeds to
    crawl + direct CBA PDF URLs) and downloads contract-type documents into the
    same raw store + manifest the normal ingest consumes.
    """
    raw = json.loads(Path(sources).read_text(encoding="utf-8"))
    src = raw.get("sources", raw)
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()} or None
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(out_dir / "manifest.jsonl")
    lines = ["## Contract crawl", ""]

    for slug, seeds in src.items():
        if slug.startswith("_") or not isinstance(seeds, list):
            continue
        if wanted and slug not in wanted:
            continue
        console.rule(slug)
        collected: list = []
        with _fetcher(
            user_agent, min_interval, respect_robots=not ignore_robots, browser=browser
        ) as f:
            for seed in seeds:
                try:
                    collected.extend(
                        docs_from_seed(f, seed, slug, max_pages=max_pages,
                                       target_only=not all_pdfs)
                    )
                except Exception as exc:  # one bad seed shouldn't sink the district
                    console.print(f"  [red]seed failed[/red] {seed}: {exc}")
            seen: set[str] = set()
            uniq = [d for d in collected
                    if d.source_url not in seen and not seen.add(d.source_url)]
            disc = out_dir / f"discovered-contracts-{slug}.jsonl"
            with disc.open("w", encoding="utf-8") as fh:
                for d in uniq:
                    fh.write(json.dumps(
                        {"doc_type": str(d.doc_type), "title": d.title, "url": d.source_url}
                    ) + "\n")
            stats = download_docs(
                uniq, fetcher=f, store=RawStore(out_dir), manifest=manifest, dry_run=dry_run
            )
        by_type = ", ".join(f"{k}={v}" for k, v in sorted(stats.by_type.items())) or "none"
        verb = "would download" if dry_run else "downloaded"
        console.print(
            f"[bold]{slug}[/bold]: {verb} {stats.downloaded} "
            f"(discovered {stats.discovered}, failed {stats.failed}); by type: {by_type}"
        )
        lines.append(f"- **{slug}**: {verb} {stats.downloaded} "
                     f"(discovered {stats.discovered}, failed {stats.failed}); {by_type}")

    if report:
        Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        console.print(f"\nreport: {report}")
    if not dry_run:
        console.print(f"manifest: {out_dir / 'manifest.jsonl'}")


if __name__ == "__main__":
    app()


@app.command("policy-probe")
def policy_probe(
    targets: str = typer.Option(
        "data/targets/policy_portals.json", help="Policy portal targets JSON."
    ),
    only: str | None = typer.Option(
        None, help="Only these district slug(s), comma-separated."
    ),
    out: str = typer.Option("data/probe/policy", help="Where to save captured bodies."),
    report: str | None = typer.Option(None, help="Write a markdown summary here."),
    ignore_robots: bool = typer.Option(True, help="Bypass robots.txt (public records)."),
    browser: bool = typer.Option(True, help="Present as a browser."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(2.0, help="Min seconds between requests."),
) -> None:
    """Find each district's policy-manual portal and capture what it returns.

    Reconnaissance only — no downloads, no parsing. The adopted policy manual
    is served by third-party portals (boardpolicyonline / microscribepub) that
    sit outside this project's egress allowlist, so the structure has to be
    captured from a networked runner before a parser can be written against
    it (same path the BoardDocs API took; docs/SCRAPING.md).
    """
    from herald.scrape.policy import (
        discover_portal,
        load_portal_targets,
        probe_portal,
        render_report,
        vendor_of,
    )

    districts = load_portal_targets(targets)
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()} or None
    out_dir = Path(out)
    probes: list = []
    discovered: dict[str, list[str]] = {}

    with _fetcher(
        user_agent, min_interval, respect_robots=not ignore_robots, browser=browser
    ) as f:
        for slug, cfg in districts.items():
            if slug.startswith("_") or (wanted and slug not in wanted):
                continue
            console.rule(slug)
            urls: list[str] = []
            if cfg.get("portal"):
                urls.append(cfg["portal"])
            if not urls and cfg.get("discover_from"):
                console.print(f"  discovering from {cfg['discover_from']}")
                urls = discover_portal(f, cfg["discover_from"])
                if urls:
                    discovered[slug] = urls
                    console.print(f"  [green]found[/green]: {', '.join(urls[:3])}")
                else:
                    console.print("  [yellow]no portal link found[/yellow]")
            for i, u in enumerate(urls[:3]):
                p = probe_portal(
                    f, slug, u, save_to=out_dir / f"{slug}_{i}_{vendor_of(u) or 'unknown'}.html"
                )
                probes.append(p)
                console.print(
                    f"  {u[:70]} -> {p.status or p.error[:40]} "
                    f"({p.bytes_len}b, {len(p.frames)} frames, {len(p.scripts)} scripts)"
                )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probes.json").write_text(
        json.dumps([p.as_dict() for p in probes], indent=2), encoding="utf-8"
    )
    text = render_report(probes, discovered)
    if report:
        Path(report).write_text(text, encoding="utf-8")
        console.print(f"report: {report}")
