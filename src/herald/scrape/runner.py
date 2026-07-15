"""Orchestration: drive an adapter, download, dedupe, record."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from herald.scrape.boarddocs import (
    BoardDocsClient,
    CommitteeNotFound,
    iter_documents,
)
from herald.scrape.core import (
    Fetcher,
    Manifest,
    RawStore,
    RobotsDisallowed,
    make_manifest_entry,
    sha256_bytes,
)
from herald.scrape.models import ScrapedDoc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Target:
    """One district to crawl, as listed in a targets JSON file."""

    district: str
    name: str
    state: str
    slug: str
    committees: list[str] | None = None  # explicit committee ids, if known
    note: str = ""


def load_targets(path: str | Path) -> list[Target]:
    """Read a targets JSON file (see data/targets/port_chester_peers.json)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw["targets"] if isinstance(raw, dict) else raw
    return [
        Target(
            district=r["district"],
            name=r.get("name", r["district"]),
            state=r["state"],
            slug=r["slug"],
            committees=r.get("committees"),
            note=r.get("note", ""),
        )
        for r in rows
    ]


@dataclass
class ScrapeStats:
    discovered: int = 0
    downloaded: int = 0
    skipped_seen: int = 0
    skipped_dup_content: int = 0
    skipped_robots: int = 0
    failed: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def _bump_type(self, doc_type: str) -> None:
        self.by_type[doc_type] = self.by_type.get(doc_type, 0) + 1


def _guess_ext(content_type: str | None) -> str:
    if not content_type:
        return ".bin"
    ct = content_type.split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "text/html": ".html",
        "text/plain": ".txt",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }.get(ct, ".bin")


def download_docs(
    docs: Iterable[ScrapedDoc],
    *,
    fetcher: Fetcher,
    store: RawStore,
    manifest: Manifest,
    dry_run: bool = False,
    on_event: object = None,
) -> ScrapeStats:
    """Fetch each discovered doc (unless already recorded) and persist it.

    Idempotent: a ``source_url`` already in the manifest is skipped without a
    network call, and a payload whose content-hash was already stored is
    recorded-but-not-rewritten so identical files (common with reposted PDFs)
    don't duplicate on disk.
    """
    stats = ScrapeStats()
    for doc in docs:
        stats.discovered += 1
        stats._bump_type(doc.doc_type.value)
        if callable(on_event):
            on_event("discovered", doc)

        if dry_run:
            continue
        if manifest.has_url(doc.source_url):
            stats.skipped_seen += 1
            continue

        try:
            resp = fetcher.get(doc.source_url)
            data = resp.content
        except RobotsDisallowed:
            stats.skipped_robots += 1
            logger.info("robots.txt disallows %s; skipping", doc.source_url)
            continue
        except Exception as exc:
            stats.failed += 1
            logger.warning("download failed %s: %s", doc.source_url, exc)
            continue

        sha = sha256_bytes(data)
        content_type = resp.headers.get("Content-Type")
        if manifest.has_hash(sha):
            # Same bytes already on disk under another URL — record provenance
            # but reuse the existing file rather than writing a copy.
            stats.skipped_dup_content += 1
            existing = next(
                (e for e in manifest.entries() if e.sha256 == sha), None
            )
            local_path = Path(existing.local_path) if existing else store.write(
                doc, data, default_ext=_guess_ext(content_type)
            )
        else:
            local_path = store.write(doc, data, default_ext=_guess_ext(content_type))

        manifest.append(
            make_manifest_entry(
                doc,
                local_path=local_path,
                sha256=sha,
                size_bytes=len(data),
                content_type=content_type,
            )
        )
        stats.downloaded += 1
        if callable(on_event):
            on_event("downloaded", doc)

    return stats


def crawl_target(
    client: BoardDocsClient,
    target: Target,
    *,
    store: RawStore,
    manifest: Manifest,
    since: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, ScrapeStats]:
    """Crawl one district's committees. Returns stats keyed by committee id.

    Committee ids come from the target's explicit ``committees`` if set,
    otherwise the board's id is auto-discovered from the /Public page. Raises
    ``CommitteeNotFound`` if neither yields an id.
    """
    committee_ids = list(target.committees) if target.committees else []
    if not committee_ids:
        discovered = client.discover_committee_id()
        if discovered:
            committee_ids = [discovered]
    if not committee_ids:
        raise CommitteeNotFound(
            f"no committee id for {target.slug}: not in targets, and none found "
            f"on {client.public_url}"
        )

    out: dict[str, ScrapeStats] = {}
    for cid in committee_ids:
        docs = iter_documents(
            client,
            district=target.district,
            committee=cid,
            committee_name=cid,
            since=since,
            limit=limit,
        )
        out[cid] = download_docs(
            docs, fetcher=client.fetcher, store=store, manifest=manifest, dry_run=dry_run
        )
    return out


@dataclass
class DistrictResult:
    """Per-district outcome of a batch crawl, for reporting."""

    name: str
    state: str
    slug: str
    status: str  # "ok" | "skipped" | "no-match"
    error: str | None = None
    committees: dict[str, ScrapeStats] = field(default_factory=dict)


def render_report(results: list[DistrictResult], *, dry_run: bool) -> str:
    """Render a batch crawl as GitHub-flavored markdown (phone-readable)."""
    verb = "Would DL" if dry_run else "Downloaded"
    title = "Crawl report" + (" — dry run (nothing downloaded)" if dry_run else "")
    lines = [f"## {title}", ""]
    lines.append(f"| District | Status | Committee | Discovered | {verb} | Skipped | Failed |")
    lines.append("|---|---|---|--:|--:|--:|--:|")
    attention: list[DistrictResult] = []
    for r in results:
        if r.status != "ok":
            lines.append(f"| {r.name} | **{r.status}** | — | — | — | — | — |")
            attention.append(r)
            continue
        for cid, s in r.committees.items():
            lines.append(
                f"| {r.name} | ok | {cid} | {s.discovered} | "
                f"{s.downloaded} | {s.skipped_seen} | {s.failed} |"
            )
    if attention:
        lines += ["", "### Needs attention", ""]
        for r in attention:
            detail = (
                f"{r.error or 'skipped'}. Confirm the slug at "
                "go.boarddocs.com/&lt;state&gt;/&lt;slug&gt; or add a committee id to the "
                "targets file."
            )
            lines.append(f"- **{r.name}** (`{r.state}/{r.slug}`): {detail}")
    return "\n".join(lines) + "\n"
