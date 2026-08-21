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
import logging
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
    RETRY_STATUSES_WITH_FORBIDDEN,
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
logger = logging.getLogger(__name__)

CONTACT_EMAIL = "timhartnett29@gmail.com"


def _fetcher(
    user_agent: str,
    min_interval: float,
    respect_robots: bool = True,
    browser: bool = True,
    **retry: object,
) -> Fetcher:
    """``retry`` passes ``retry_statuses`` / ``max_retries`` /
    ``retry_base_delay`` through to the Fetcher for hosts that need it."""
    if browser:
        # Present as a browser (some hosts 403 non-browser clients) while
        # keeping an honest contact via the From header + polite rate limit.
        return Fetcher(
            user_agent=BROWSER_USER_AGENT,
            headers={**BROWSER_HEADERS, "From": CONTACT_EMAIL},
            min_request_interval=min_interval,
            respect_robots=respect_robots,
            **retry,  # type: ignore[arg-type]
        )
    return Fetcher(
        user_agent=user_agent,
        min_request_interval=min_interval,
        respect_robots=respect_robots,
        **retry,  # type: ignore[arg-type]
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


@app.command("policy-fetch")
def policy_fetch(
    targets: str = typer.Option(
        "data/targets/policy_portals.json", help="Policy portal targets JSON."
    ),
    only: str | None = typer.Option(
        None, help="Only these district slug(s), comma-separated."
    ),
    out: str = typer.Option("data/raw", help="Root dir for the raw store."),
    report: str | None = typer.Option(None, help="Write a markdown summary here."),
    save_export: bool = typer.Option(
        True, help="Also keep each district's whole-manual export HTML."
    ),
    keep_dividers: bool = typer.Option(
        False, help="Also store section dividers (a title with no policy text)."
    ),
    dry_run: bool = typer.Option(False, help="Fetch + split, but write nothing."),
    headless: bool = typer.Option(True, help="Run the browser headless."),
    chromium: str | None = typer.Option(
        None, help="Explicit Chromium binary (e.g. /opt/pw-browsers/chromium)."
    ),
    proxy: str | None = typer.Option(None, help="Proxy for the browser, e.g. http://host:port."),
    tls12: bool = typer.Option(
        False, help="Cap TLS at 1.2 (some egress middleboxes reset Chromium's TLS 1.3)."
    ),
    export_timeout: float = typer.Option(180.0, help="Seconds to wait for the server export."),
) -> None:
    """Pull whole adopted policy manuals out of the BoardPolicyOnline portals.

    The manual is a Blazor Server app with no fetchable API — see
    ``herald.scrape.policy_manual``. This drives the portal's own bulk export
    (select the whole tree, Print), splits the returned HTML into one document
    per policy, and files each into the same raw store + manifest the normal
    ingest consumes. Every policy keeps the portal's own deep link
    (``/b/<slug>/s/<id>``) as its ``source_url``, so answers can cite it.
    """
    from herald.scrape.core import make_manifest_entry, sha256_bytes
    from herald.scrape.models import DocType, ScrapedDoc
    from herald.scrape.policy import load_portal_targets
    from herald.scrape.policy_manual import BrowserOptions, fetch_manual_export, split_export

    districts = load_portal_targets(targets)
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()} or None
    out_dir = Path(out)
    manifest = Manifest(out_dir / "manifest.jsonl")
    store = RawStore(out_dir)
    opts = BrowserOptions(
        headless=headless,
        executable_path=chromium,
        proxy={"server": proxy} if proxy else None,
        tls12=tls12,
        export_timeout=export_timeout,
    )
    lines = ["## Policy manual fetch", "",
             "| district | sections | policies | stored | skipped (seen) | note |",
             "|---|---:|---:|---:|---:|---|"]

    for slug, cfg in districts.items():
        if slug.startswith("_") or (wanted and slug not in wanted):
            continue
        console.rule(slug)
        if cfg.get("vendor") != "boardpolicyonline" or not cfg.get("portal"):
            note = f"vendor={cfg.get('vendor') or 'unknown'} — not supported yet"
            console.print(f"  [yellow]skip[/yellow]: {note}")
            lines.append(f"| {slug} | - | - | - | - | {note} |")
            continue
        try:
            html, final_url = fetch_manual_export(cfg["portal"], options=opts)
        except Exception as exc:
            console.print(f"  [red]export failed[/red]: {exc}")
            lines.append(f"| {slug} | - | - | - | - | export failed: {str(exc)[:60]} |")
            continue

        docs = split_export(html, portal_url=final_url)
        keep = [d for d in docs if d.is_substantive or keep_dividers]
        console.print(
            f"  export {len(html):,} chars -> {len(docs)} sections, "
            f"{sum(1 for d in docs if d.is_substantive)} with policy text"
        )

        if save_export and not dry_run:
            raw_path = out_dir / slug / "policy" / "_manual_export.html"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(html, encoding="utf-8")

        stored = skipped = 0
        for d in keep:
            body = d.html.encode("utf-8")
            sha = sha256_bytes(body)
            # Keyed on (url, content): two policies can share a body — a
            # district may adopt the same template text under two numbers —
            # and both are still real policies with their own permalink.
            if manifest.has_url_hash(d.source_url, sha):
                skipped += 1
                continue
            if dry_run:
                stored += 1
                continue
            doc = ScrapedDoc(
                district=slug,
                doc_type=DocType.policy,
                title=d.title or f"section {d.section_id}",
                source_url=d.source_url,
                suggested_filename=f"{d.number or d.section_id}.html",
            )
            path = store.write(doc, body, default_ext=".html")
            manifest.append(make_manifest_entry(
                doc, local_path=path, sha256=sha, size_bytes=len(body),
                content_type="text/html",
            ))
            stored += 1
        verb = "would store" if dry_run else "stored"
        console.print(f"  {verb} {stored}, skipped {skipped} already in the manifest")
        lines.append(
            f"| {slug} | {len(docs)} | {len(keep)} | {stored} | {skipped} | {verb} |"
        )

    text = "\n".join(lines) + "\n"
    if report:
        Path(report).write_text(text, encoding="utf-8")
        console.print(f"\nreport: {report}")
    if not dry_run:
        console.print(f"manifest: {out_dir / 'manifest.jsonl'}")

@app.command("policy-boarddocs")
def policy_boarddocs(
    peers: str = typer.Option(
        "data/targets/port_chester_peers.json", help="Peer set JSON (district slugs)."
    ),
    only: str | None = typer.Option(
        None, help="Only these district slug(s), comma-separated."
    ),
    out: str = typer.Option("data/raw", help="Root dir for the raw store."),
    report: str | None = typer.Option(None, help="Write a markdown summary here."),
    dry_run: bool = typer.Option(False, help="List the manual, but write nothing."),
    limit: int | None = typer.Option(None, help="Stop after N policies per district."),
    portals: str = typer.Option(
        "data/targets/policy_portals.json",
        help="Portal targets; districts with a portal are skipped (see --skip-portal).",
    ),
    attachments: bool = typer.Option(
        True,
        help="Also download files attached to a policy — sometimes the whole policy.",
    ),
    skip_portal: bool = typer.Option(
        True,
        help="Skip districts whose manual comes from an external portal, so the "
             "corpus never holds two copies of one policy.",
    ),
    resume: bool = typer.Option(
        True,
        help="Skip policies already in the manifest WITHOUT re-requesting them, so a "
             "run that was cut short can be finished by running again.",
    ),
    ignore_robots: bool = typer.Option(True, help="Bypass robots.txt (public records)."),
    user_agent: str = typer.Option(BROWSER_USER_AGENT),
    min_interval: float = typer.Option(1.0, help="Min seconds between requests."),
    retry_base_delay: float = typer.Option(
        5.0, help="First backoff after a rate-trip, doubling each retry."
    ),
    max_retries: int = typer.Option(5, help="Retries per request before giving up."),
) -> None:
    """Pull adopted policy manuals out of the BoardDocs policy console.

    The other half of the policy corpus. Four peer districts have no external
    portal but *do* serve their whole manual through BoardDocs'
    ``BD-GetPolicyBooks`` / ``BD-GetPolicies`` / ``BD-GetPolicyItem``
    endpoints. Those looked locked — every call answered ``No Access`` — but
    that was the ``status`` parameter, not authorization: ``status=active``
    returns the full index anonymously.

    Each policy is stored with the district's own permalink
    (``Board.nsf/goto?open&id=<unique>``) as its ``source_url``, and with the
    console's ``Adopted`` / ``Last Reviewed`` dates, which the portal manuals
    only ever bury in prose.
    """
    from herald.scrape.boarddocs import (
        PolicyAccessDenied,
        choose_policy_books,
        filename_of,
    )
    from herald.scrape.core import make_manifest_entry, sha256_bytes
    from herald.scrape.models import DocType, ScrapedDoc
    from herald.scrape.policy import load_portal_targets

    targets = load_targets(peers)
    # Ossining answers here too, but with a stale 12-policy book against the
    # 434 its portal serves. Two copies of one policy is worse than one.
    on_portal = {
        slug for slug, cfg in load_portal_targets(portals).items() if cfg.get("portal")
    } if skip_portal else set()
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()} or None
    out_dir = Path(out)
    manifest = Manifest(out_dir / "manifest.jsonl")
    store = RawStore(out_dir)
    lines = ["## BoardDocs policy manuals", "",
             "| district | books | policies | stored | skipped (seen) | note |",
             "|---|---|---:|---:|---:|---|"]
    # A district that came back with nothing at all. Collected rather than
    # raised, so one blocked district never costs us the other three — but
    # reported as a non-zero exit at the end, because the previous run
    # returned a green check over 20 policies out of 1,077.
    broken: list[str] = []

    for t in targets:
        slug = t.district
        if wanted and slug not in wanted:
            continue
        console.rule(slug)
        if slug in on_portal:
            console.print("  [dim]skip[/dim]: covered by herald-scrape policy-fetch")
            lines.append(f"| {slug} | - | - | - | - | on external portal |")
            continue
        with _fetcher(
            user_agent, min_interval, respect_robots=not ignore_robots, browser=True,
            # BoardDocs answers a rate-tripped client with 403, not 429. Read
            # literally that is "never"; in practice the block lifts, so back
            # off and try again rather than discarding the policy.
            retry_statuses=RETRY_STATUSES_WITH_FORBIDDEN,
            retry_base_delay=retry_base_delay,
            max_retries=max_retries,
        ) as f:
            client = BoardDocsClient(state=t.state, slug=t.slug, fetcher=f)
            try:
                books = client.list_policy_books()
            except Exception as exc:
                console.print(f"  [red]book list failed[/red]: {exc}")
                lines.append(f"| {slug} | - | - | - | - | books failed: {str(exc)[:50]} |")
                broken.append(f"{slug}: book list failed ({str(exc)[:60]})")
                continue
            if not books:
                # Not a failure: this district's manual is on an external
                # portal instead (herald-scrape policy-fetch covers those).
                console.print("  [yellow]no policy books[/yellow] — external portal district")
                lines.append(f"| {slug} | 0 | - | - | - | external portal |")
                continue
            chosen = choose_policy_books(books)
            console.print(f"  books: {books} -> using {chosen}")

            refs = []
            for book in chosen:
                try:
                    refs.extend(client.list_policies(book))
                except PolicyAccessDenied as exc:
                    console.print(f"  [red]{exc}[/red]")
            if limit:
                refs = refs[:limit]
            console.print(f"  {len(refs)} policies in the index")

            stored = skipped = failed = empty = attached = 0
            for i, ref in enumerate(refs, 1):
                url = client.policy_url(ref.unique)
                if resume and manifest.has_url(url):
                    # Already collected by an earlier run. Skipping before the
                    # request is the point: a run that was cut off partway
                    # resumes where it stopped instead of spending its whole
                    # rate budget re-fetching what it already has.
                    skipped += 1
                    continue
                try:
                    item = client.get_policy(ref)
                except Exception as exc:
                    failed += 1
                    logger.warning("policy fetch failed %s: %s", ref.unique, exc)
                    continue
                title = item.display_title or ref.display_title or ref.unique
                if item.has_body:
                    body = item.body_html.encode("utf-8")
                    sha = sha256_bytes(body)
                    # Keyed on (url, content), not content alone: two policies
                    # can share a body while being two real policies with two
                    # numbers and two permalinks.
                    if manifest.has_url_hash(url, sha):
                        skipped += 1
                    elif dry_run:
                        stored += 1
                    else:
                        doc = ScrapedDoc(
                            district=slug,
                            doc_type=DocType.policy,
                            title=title,
                            source_url=url,
                            suggested_filename=f"{item.code or ref.unique}.html",
                        )
                        path = store.write(doc, body, default_ext=".html")
                        manifest.append(make_manifest_entry(
                            doc, local_path=path, sha256=sha, size_bytes=len(body),
                            content_type="text/html",
                        ))
                        stored += 1
                elif not ref.has_attachment:
                    empty += 1

                # Some policies carry their text ENTIRELY in an attachment
                # (Mount Vernon's 0115 DASA has an empty body), so this is not
                # an extra: skipping it loses whole policies.
                if not attachments or not ref.has_attachment:
                    continue
                try:
                    files = client.get_policy_files(ref)
                except Exception as exc:
                    logger.warning("attachment list failed %s: %s", ref.unique, exc)
                    continue
                for fref in files:
                    if dry_run:
                        attached += 1
                        continue
                    try:
                        resp = f.get(fref.url)
                        blob = resp.content
                    except Exception as exc:
                        logger.warning("attachment fetch failed %s: %s", fref.url, exc)
                        continue
                    fsha = sha256_bytes(blob)
                    if manifest.has_url_hash(fref.url, fsha):
                        continue
                    fdoc = ScrapedDoc(
                        district=slug,
                        doc_type=DocType.policy,
                        title=f"{title} (attachment: {fref.title})"[:300],
                        source_url=fref.url,
                        # The URL carries the real filename; the link text
                        # often appends a size ("… .docx (22 KB)"), which
                        # would store a Word file under a bogus extension and
                        # send it to the PDF reader.
                        suggested_filename=filename_of(fref.url),
                    )
                    fpath = store.write(fdoc, blob, default_ext=".pdf")
                    manifest.append(make_manifest_entry(
                        fdoc, local_path=fpath, sha256=fsha, size_bytes=len(blob),
                        content_type=resp.headers.get("Content-Type"),
                    ))
                    attached += 1
                if i % 50 == 0:
                    console.print(f"  [{i}/{len(refs)}] {title[:60]}")

        if refs and not (stored or skipped):
            broken.append(f"{slug}: every one of {len(refs)} policies failed")
        verb = "would store" if dry_run else "stored"
        bits = [verb]
        if attached:
            bits.append(f"{attached} attachment(s)")
        if empty:
            bits.append(f"{empty} with no body and no attachment")
        if failed:
            bits.append(f"{failed} fetch failure(s)")
        note = "; ".join(bits)
        console.print(
            f"  {verb} {stored}, attachments {attached}, skipped {skipped}, "
            f"empty {empty}, failed {failed}"
        )
        lines.append(
            f"| {slug} | {', '.join(chosen)} | {len(refs)} | {stored} | {skipped} | {note} |"
        )

    if broken:
        lines += ["", "**Districts that returned nothing:**", ""]
        lines += [f"- {b}" for b in broken]
    text = "\n".join(lines) + "\n"
    if report:
        Path(report).write_text(text, encoding="utf-8")
        console.print(f"\nreport: {report}")
    if not dry_run:
        console.print(f"manifest: {out_dir / 'manifest.jsonl'}")
    if broken:
        console.print(
            f"\n[red]{len(broken)} district(s) returned nothing[/red] — run again to "
            "resume; policies already stored are skipped without a request."
        )
        for b in broken:
            console.print(f"  [red]\u00b7[/red] {b}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
