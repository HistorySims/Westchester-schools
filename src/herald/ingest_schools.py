"""Schools ingest: scrape manifest → PDF text → structural chunks → embed → Postgres.

The acquisition layer (``herald.scrape``) leaves behind raw files plus an
append-only ``manifest.jsonl``; this module consumes that contract. Per
document: extract text (PyMuPDF), fix the authoritative ``meeting_date`` /
``doc_type`` from content (scrape-time values are provisional), chunk on
the agenda's own outline (``herald.chunking``), embed with a deterministic
contextual prefix (docs/CHUNKING.md "Embedding strategy"), and write
document + chunks in one transaction so re-runs are resumable: a document
is only marked ``ingested`` when its chunks committed.

``--dry-run`` (the default) needs no database and no Voyage key — it
extracts + chunks and reports what a real run would write.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from herald.chunking import Chunk, chunk_agenda_text, classify_doc_type, parse_meeting_date
from herald.embed import VoyageEmbedder
from herald.pdf_text import extract_pdf_text
from herald.scrape.models import ManifestEntry

logger = logging.getLogger(__name__)
console = Console()

MIN_TEXT_CHARS = 200   # below this the "PDF" is likely scanned/empty
MIN_CHUNK_CHARS = 40   # drop fragments too small to mean anything
DEFAULT_WAVE = 512     # chunks buffered before an embed+write flush


# ---- manifest loading --------------------------------------------------

def find_manifests(root: str | Path) -> list[Path]:
    """Every ``manifest.jsonl`` under ``root`` (one per scrape artifact)."""
    return sorted(Path(root).glob("**/manifest.jsonl"))


def load_manifest(path: Path) -> list[ManifestEntry]:
    out: list[ManifestEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(ManifestEntry.model_validate_json(line))
    return out


def resolve_local_path(entry: ManifestEntry, manifest_path: Path) -> Path | None:
    """Find the downloaded file on *this* filesystem.

    ``local_path`` was recorded where the scrape ran (e.g. an Actions
    runner as ``data/raw/<district>/<doc_type>/<file>``); after the
    artifact is downloaded elsewhere only the tail is stable, and the
    RawStore layout guarantees the file sits next to its manifest as
    ``<district>/<doc_type>/<file>``.
    """
    p = Path(entry.local_path)
    if p.is_file():
        return p
    if len(p.parts) >= 3:
        q = manifest_path.parent / Path(*p.parts[-3:])
        if q.is_file():
            return q
    return None


# ---- chunk preparation -------------------------------------------------

def embed_input(chunk: Chunk) -> str:
    """The text actually sent to the embedder: contextual breadcrumb + body.

    A chunk pulled out of its document loses the context that it is, say,
    a finance contract from Peekskill in March 2026; the prefix restores
    it (contextual retrieval, deterministic form). Stored ``content``
    stays the raw body — only the embedding sees the prefix.
    """
    date_s = chunk.meeting_date.isoformat() if chunk.meeting_date else "undated"
    head = chunk.heading or ""
    crumb = f"{chunk.district} · {date_s} · {chunk.section_type}"
    if head and head != chunk.section_type:
        crumb += f" \u203a {head}"
    return f"{crumb}\n\n{chunk.content}"


def prepare_document(entry: ManifestEntry, text: str) -> tuple[list[Chunk], _dt.date | None, str]:
    """Chunk one document's text; returns (chunks, meeting_date, doc_type).

    The scrape-time date is a placeholder on some sources (BoardDocs stamps
    the school-year end on every file), so the title and the document header
    are authoritative and the manifest date is only a last resort.
    """
    meeting_date = (
        parse_meeting_date(entry.title)
        or parse_meeting_date(text[:2000])
        or entry.date
    )
    doc_type = str(entry.doc_type)
    if doc_type == "other":
        doc_type = classify_doc_type(entry.title)
    chunks = chunk_agenda_text(
        text,
        district=entry.district,
        meeting_date=meeting_date,
        doc_type=doc_type,
        source_url=entry.source_url,
    )
    return [c for c in chunks if len(c.content) >= MIN_CHUNK_CHARS], meeting_date, doc_type


# ---- orchestration -----------------------------------------------------

@dataclass
class IngestStats:
    docs_seen: int = 0
    docs_skipped: int = 0      # already ingested (manifest re-run)
    docs_missing: int = 0      # file not found next to its manifest
    docs_no_text: int = 0      # scanned/empty PDF
    docs_error: int = 0
    docs_ingested: int = 0
    chunks_written: int = 0
    by_district: Counter[str] = field(default_factory=Counter)
    by_doc_type: Counter[str] = field(default_factory=Counter)


@dataclass
class _DocWork:
    entry: ManifestEntry
    chunks: list[Chunk]
    meeting_date: _dt.date | None
    doc_type: str
    page_count: int
    text_chars: int
    document_id: object = None  # UUID when writing to the DB


async def ingest_manifests(
    pairs: list[tuple[ManifestEntry, Path]],
    *,
    conn=None,                       # psycopg connection, or None for dry-run
    voyage: VoyageEmbedder | None = None,
    wave_size: int = DEFAULT_WAVE,
    on_doc=None,                     # callback(entry, status) for progress
) -> IngestStats:
    """Ingest manifest entries. ``conn is None`` means dry-run (no writes)."""
    from herald import schools_db

    stats = IngestStats()
    districts: dict[str, object] = {}   # slug -> district UUID
    wave: list[_DocWork] = []

    def district_id(slug: str):
        if slug not in districts:
            with conn.transaction():
                districts[slug] = schools_db.upsert_district(conn.cursor(), slug=slug)
        return districts[slug]

    async def flush() -> None:
        if not wave:
            return
        all_chunks = [c for w in wave for c in w.chunks]
        vectors: list[list[float] | None] = [None] * len(all_chunks)
        if voyage is not None:
            vectors = await voyage.embed_documents([embed_input(c) for c in all_chunks])
        if conn is not None:
            i = 0
            for w in wave:
                rows = []
                for c in w.chunks:
                    rows.append(schools_db.SchoolChunkRow(
                        chunk_index=c.order_index,
                        section_path=c.section_path,
                        section_type=c.section_type,
                        heading=c.heading,
                        content=c.content,
                        embedding=vectors[i],
                        meeting_date=c.meeting_date,
                        doc_type=c.doc_type,
                    ))
                    i += 1
                with conn.transaction():
                    cur = conn.cursor()
                    schools_db.insert_chunks(
                        cur,
                        document_id=w.document_id,
                        district_id=districts[w.entry.district],
                        rows=rows,
                    )
                    schools_db.mark_document(
                        cur,
                        document_id=w.document_id,
                        status="ingested",
                        meeting_date=w.meeting_date,
                        doc_type=w.doc_type,
                        page_count=w.page_count,
                        text_chars=w.text_chars,
                    )
        for w in wave:
            stats.docs_ingested += 1
            stats.chunks_written += len(w.chunks)
            stats.by_district[w.entry.district] += len(w.chunks)
            stats.by_doc_type[w.doc_type] += len(w.chunks)
        wave.clear()

    def mark(document_id, status: str, error: str | None = None) -> None:
        if conn is None or document_id is None:
            return
        with conn.transaction():
            schools_db.mark_document(
                conn.cursor(), document_id=document_id, status=status, error=error
            )

    for entry, manifest_path in pairs:
        stats.docs_seen += 1
        note = "ok"
        doc_id = None
        try:
            if conn is not None:
                doc_id, existing = schools_db.find_or_insert_document(
                    conn.cursor(),
                    district_id=district_id(entry.district),
                    doc_type=str(entry.doc_type),
                    title=entry.title,
                    source_url=entry.source_url,
                    sha256=entry.sha256,
                    size_bytes=entry.size_bytes,
                    content_type=entry.content_type,
                    local_path=entry.local_path,
                    committee=entry.committee,
                    meeting_id=entry.meeting_id,
                    meeting_date=entry.date,
                    fetched_at=entry.fetched_at,
                )
                conn.commit()
                if existing == "ingested":
                    stats.docs_skipped += 1
                    note = "skipped"
                    continue

            path = resolve_local_path(entry, manifest_path)
            if path is None:
                stats.docs_missing += 1
                note = "missing"
                mark(doc_id, "error", error=f"file not found: {entry.local_path}")
                continue

            try:
                extracted = extract_pdf_text(path)
            except Exception as exc:
                stats.docs_error += 1
                note = f"error: {exc}"
                logger.warning("extract failed %s: %s", path, exc)
                mark(doc_id, "error", error=str(exc)[:500])
                continue

            chunks, meeting_date, doc_type = prepare_document(entry, extracted.text)
            if len(extracted.text) < MIN_TEXT_CHARS or not chunks:
                stats.docs_no_text += 1
                note = "no_text"
                mark(doc_id, "no_text")
                continue

            wave.append(_DocWork(
                entry=entry, chunks=chunks, meeting_date=meeting_date,
                doc_type=doc_type, page_count=extracted.page_count,
                text_chars=len(extracted.text), document_id=doc_id,
            ))
            if sum(len(w.chunks) for w in wave) >= wave_size:
                await flush()
        finally:
            if on_doc is not None:
                on_doc(entry, note)

    await flush()
    return stats


# ---- reporting ---------------------------------------------------------

def render_report(stats: IngestStats, *, dry_run: bool) -> str:
    mode = "DRY RUN — nothing written" if dry_run else "ingested to database"
    lines = [
        "# Ingest report",
        "",
        f"_{mode}_",
        "",
        "| docs seen | ingested | skipped | no text | missing | errors | chunks |",
        "|---|---|---|---|---|---|---|",
        f"| {stats.docs_seen} | {stats.docs_ingested} | {stats.docs_skipped} "
        f"| {stats.docs_no_text} | {stats.docs_missing} | {stats.docs_error} "
        f"| {stats.chunks_written} |",
        "",
        "## Chunks by district",
        "",
        "| district | chunks |",
        "|---|---|",
    ]
    lines += [f"| {d} | {n} |" for d, n in stats.by_district.most_common()]
    lines += ["", "## Chunks by doc type", "", "| doc_type | chunks |", "|---|---|"]
    lines += [f"| {t} | {n} |" for t, n in stats.by_doc_type.most_common()]
    return "\n".join(lines) + "\n"


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Ingest scraped school documents into the corpus DB.",
                  no_args_is_help=True)


def _db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    return url


@app.command("init-db")
def init_db(
    schema: str = typer.Option(
        "db/migrations/0001_schools_init.sql", help="Schema SQL file to apply."
    ),
) -> None:
    """Apply the schools schema to $SUPABASE_DB_URL (idempotent)."""
    from herald import schools_db

    sql = Path(schema).read_text(encoding="utf-8")
    with schools_db.connect(_db_url()) as conn:
        conn.execute(sql)
        conn.commit()
    console.print(f"[green]applied[/green] {schema}")


@app.command()
def run(
    root: str = typer.Option(
        "data", help="Directory searched for **/manifest.jsonl (scrape artifacts)."
    ),
    manifest: str | None = typer.Option(
        None, help="Explicit manifest path(s), comma-separated; adds to --root's finds."
    ),
    district: str | None = typer.Option(None, help="Only ingest this district slug."),
    doc_type: str | None = typer.Option(None, help="Only ingest this doc type."),
    limit: int | None = typer.Option(None, help="Stop after N manifest entries."),
    dry_run: bool = typer.Option(
        True, help="Extract + chunk + report only; no DB, no Voyage."
    ),
    wave_size: int = typer.Option(DEFAULT_WAVE, help="Chunks per embed/write flush."),
    report: str | None = typer.Option(None, help="Write a markdown report here."),
) -> None:
    """Ingest every document recorded in the scrape manifests."""
    explicit = [Path(m.strip()) for m in (manifest or "").split(",") if m.strip()]
    manifests = explicit + find_manifests(root)
    manifests = list(dict.fromkeys(manifests))  # de-dupe, keep order
    if not manifests:
        console.print(f"[red]no manifest.jsonl found under {root!r}[/red]")
        raise typer.Exit(1)

    pairs: list[tuple[ManifestEntry, Path]] = []
    for mpath in manifests:
        for entry in load_manifest(mpath):
            if district and entry.district != district:
                continue
            if doc_type and str(entry.doc_type) != doc_type:
                continue
            pairs.append((entry, mpath))
    if limit is not None:
        pairs = pairs[:limit]
    console.print(f"{len(pairs)} document(s) across {len(manifests)} manifest(s)")

    conn = None
    voyage = None
    if not dry_run:
        from herald import schools_db

        conn = schools_db.connect(_db_url())
        key = os.environ.get("VOYAGE_API_KEY", "")
        if not key:
            raise typer.BadParameter("VOYAGE_API_KEY is not set.")
        voyage = VoyageEmbedder(key)

    done = 0

    def on_doc(entry: ManifestEntry, note: str) -> None:
        nonlocal done
        done += 1
        if done % 25 == 0 or note not in ("ok", "skipped"):
            console.print(f"[{done}/{len(pairs)}] {entry.district} {entry.title[:60]!r} {note}")

    async def go() -> IngestStats:
        try:
            return await ingest_manifests(
                pairs, conn=conn, voyage=voyage, wave_size=wave_size, on_doc=on_doc
            )
        finally:
            if voyage is not None:
                await voyage.aclose()

    try:
        stats = asyncio.run(go())
    finally:
        if conn is not None:
            conn.close()

    table = Table(title="Ingest" + (" (dry run)" if dry_run else ""))
    for col in ("seen", "ingested", "skipped", "no_text", "missing", "errors", "chunks"):
        table.add_column(col, justify="right")
    table.add_row(
        str(stats.docs_seen), str(stats.docs_ingested), str(stats.docs_skipped),
        str(stats.docs_no_text), str(stats.docs_missing), str(stats.docs_error),
        str(stats.chunks_written),
    )
    console.print(table)
    for d, n in stats.by_district.most_common():
        console.print(f"  {d}: {n} chunks")

    if report:
        Path(report).write_text(render_report(stats, dry_run=dry_run), encoding="utf-8")
        console.print(f"report: {report}")


if __name__ == "__main__":
    app()
