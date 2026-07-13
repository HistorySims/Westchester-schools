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

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from herald.scrape.boarddocs import BoardDocsClient, iter_documents
from herald.scrape.core import DEFAULT_USER_AGENT, Fetcher, Manifest, RawStore
from herald.scrape.runner import download_docs

app = typer.Typer(help="Scrape district sources into raw files + a manifest.", no_args_is_help=True)
console = Console()


def _fetcher(user_agent: str, min_interval: float) -> Fetcher:
    return Fetcher(user_agent=user_agent, min_request_interval=min_interval)


@app.command()
def committees(
    state: str = typer.Option(..., help="BoardDocs state slug, e.g. 'ny'."),
    slug: str = typer.Option(..., help="District slug in the BoardDocs URL."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT, help="Identifying User-Agent."),
    min_interval: float = typer.Option(1.0, help="Min seconds between requests."),
) -> None:
    """List a district's BoardDocs committees (find the id you want)."""
    with _fetcher(user_agent, min_interval) as fetcher:
        client = BoardDocsClient(state=state, slug=slug, fetcher=fetcher)
        rows = client.list_committees()
    table = Table("unique", "name", title=f"{state}/{slug} committees")
    for c in rows:
        table.add_row(c.unique, c.name)
    console.print(table)


@app.command()
def meetings(
    state: str = typer.Option(...),
    slug: str = typer.Option(...),
    committee: str = typer.Option(..., help="Committee 'unique' id from `committees`."),
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(1.0),
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
    user_agent: str = typer.Option(DEFAULT_USER_AGENT),
    min_interval: float = typer.Option(1.0),
) -> None:
    """Discover a committee's attachments and download them."""
    since_date = date.fromisoformat(since) if since else None
    out_dir = Path(out)
    mpath = Path(manifest_path) if manifest_path else out_dir / "manifest.jsonl"
    store = RawStore(out_dir)
    manifest = Manifest(mpath)

    with _fetcher(user_agent, min_interval) as fetcher:
        client = BoardDocsClient(state=state, slug=slug, fetcher=fetcher)
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


if __name__ == "__main__":
    app()
