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
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from herald.chunking import Chunk, chunk_agenda_text, classify_doc_type, parse_meeting_date
from herald.embed import VoyageEmbedder
from herald.pdf_text import ExtractedDoc, TableBlock, extract_pdf
from herald.scrape.models import ManifestEntry

logger = logging.getLogger(__name__)
console = Console()

MIN_TEXT_CHARS = 200   # below this the "PDF" is likely scanned/empty
MIN_CHUNK_CHARS = 40   # drop fragments too small to mean anything
TABLE_MAX_CHARS = 24000  # safety cap on a single table chunk (well above real grids)
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


def _table_chunks(
    tables: list[TableBlock], *, start_order: int, **doc_meta: object
) -> list[Chunk]:
    """One whole-table chunk per detected grid (kind='table').

    Kept intact — no MIN_CHUNK_CHARS floor, no window-splitting — so retrieval
    finds the whole grid and structured extraction (docs/STRUCTURED.md) reads a
    header-bearing table. ``order_index`` continues past the prose chunks so it
    stays unique per document (the chunks PK is (document_id, chunk_index))."""
    out: list[Chunk] = []
    for i, tb in enumerate(tables):
        content = tb.markdown[:TABLE_MAX_CHARS]
        if not content.strip():
            continue
        out.append(Chunk(
            content=content,
            section_path=f"T{tb.page}#{i + 1}",
            section_type="Table",
            heading=f"Table (p. {tb.page})",
            order_index=start_order + i,
            kind="table",
            **doc_meta,  # type: ignore[arg-type]
        ))
    return out


def prepare_document(
    entry: ManifestEntry, text: str, tables: list[TableBlock] | None = None,
) -> tuple[list[Chunk], _dt.date | None, str]:
    """Chunk one document; returns (chunks, meeting_date, doc_type).

    Prose is chunked on the agenda outline; each detected table is appended as
    a single ``kind='table'`` chunk. The scrape-time date is a placeholder on
    some sources (BoardDocs stamps the school-year end on every file), so the
    title and the document header are authoritative and the manifest date is
    only a last resort.
    """
    meeting_date = (
        parse_meeting_date(entry.title)
        or parse_meeting_date(text[:2000])
        or entry.date
    )
    doc_type = str(entry.doc_type)
    if doc_type == "other":
        doc_type = classify_doc_type(entry.title)
    doc_meta = dict(
        district=entry.district,
        meeting_date=meeting_date,
        doc_type=doc_type,
        source_url=entry.source_url,
    )
    chunks = chunk_agenda_text(text, **doc_meta)
    prose = [c for c in chunks if len(c.content) >= MIN_CHUNK_CHARS]
    # Table order_index continues past *every* prose chunk emitted (including
    # ones filtered out above), so table chunk_index never collides with prose.
    next_order = max((c.order_index for c in chunks), default=-1) + 1
    tables_out = _table_chunks(tables or [], start_order=next_order, **doc_meta)
    return prose + tables_out, meeting_date, doc_type


# ---- orchestration -----------------------------------------------------

@dataclass
class IngestStats:
    docs_seen: int = 0
    docs_skipped: int = 0      # already ingested (manifest re-run)
    docs_missing: int = 0      # file not found next to its manifest
    docs_no_text: int = 0      # scanned/empty PDF (OCR mode: OCR recovered nothing)
    docs_error: int = 0
    docs_ingested: int = 0
    docs_ocr_candidate: int = 0     # OCR dry-run: a no-text doc that would be OCR'd
    docs_tables_backfilled: int = 0  # tables-only: ingested docs that gained table chunks
    docs_no_tables: int = 0          # tables-only: ingested docs with no detectable table
    chunks_written: int = 0
    by_district: Counter[str] = field(default_factory=Counter)
    by_doc_type: Counter[str] = field(default_factory=Counter)
    ocr_candidates: Counter[str] = field(default_factory=Counter)  # per district


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
    ocr_mode: bool = False,          # only (re)process no-text docs, via OCR
    ocr_fn=None,                     # callable(path)->ExtractedText; None = dry count
    tables_only: bool = False,       # backfill: add table chunks to already-ingested docs
) -> IngestStats:
    """Ingest manifest entries. ``conn is None`` means dry-run (no writes).

    In ``ocr_mode`` the roles invert: documents that already have a text
    layer are skipped (they're ingested), and only the no-text ones are
    acted on. With ``ocr_fn`` set they're OCR'd, chunked, embedded and
    written; with ``ocr_fn=None`` (the fast dry pass) they're merely
    tallied as candidates so you can see the per-district count without
    paying for OCR.

    In ``tables_only`` mode the roles invert the other way: only *already-
    ingested* documents are processed, and only their ``kind='table'`` chunks
    are embedded and inserted — a non-destructive backfill that adds whole-table
    chunks to a corpus ingested before table-aware chunking existed. Existing
    prose chunks, their embeddings, scores and cluster assignments are untouched
    (``insert_chunks`` is ``on conflict do nothing``, so re-running is safe), and
    the document's ``ingested`` status is left as-is.
    """
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
                        kind=c.kind,
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
                    # Backfill leaves the already-ingested document row alone;
                    # a normal ingest stamps it 'ingested' with what it learned.
                    if not tables_only:
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
            if tables_only:
                stats.docs_tables_backfilled += 1
            else:
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
                if tables_only:
                    # backfill only augments docs already in the corpus; a
                    # not-yet-ingested doc belongs to a normal ingest pass.
                    if existing != "ingested":
                        stats.docs_skipped += 1
                        note = "not-ingested"
                        continue
                elif existing == "ingested":
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
                extracted = extract_pdf(path)
            except Exception as exc:
                stats.docs_error += 1
                note = f"error: {exc}"
                logger.warning("extract failed %s: %s", path, exc)
                mark(doc_id, "error", error=str(exc)[:500])
                continue

            if ocr_mode:
                # A table-only born-digital PDF has little prose but real table
                # content, so "has text" is judged on total recovered chars.
                if extracted.content_chars >= MIN_TEXT_CHARS:
                    stats.docs_skipped += 1
                    note = "has-text"
                    continue
                if ocr_fn is None:
                    # fast dry pass: count the candidate, don't spend on OCR
                    stats.docs_ocr_candidate += 1
                    stats.ocr_candidates[entry.district] += 1
                    note = "ocr-candidate"
                    continue
                try:
                    ocr_text = ocr_fn(path)   # ExtractedText — no table structure
                    extracted = ExtractedDoc(
                        text=ocr_text.text, tables=[], page_count=ocr_text.page_count
                    )
                except Exception as exc:
                    stats.docs_error += 1
                    note = f"ocr-error: {exc}"
                    logger.warning("ocr failed %s: %s", path, exc)
                    mark(doc_id, "error", error=str(exc)[:500])
                    continue

            chunks, meeting_date, doc_type = prepare_document(
                entry, extracted.text, extracted.tables
            )

            if tables_only:
                # Add only the whole-table chunks; leave prose (already ingested)
                # and the document row untouched.
                chunks = [c for c in chunks if c.kind == "table"]
                if not chunks:
                    stats.docs_no_tables += 1
                    note = "no-tables"
                    continue
                note = f"{len(chunks)} table(s)"
            elif extracted.content_chars < MIN_TEXT_CHARS or not chunks:
                stats.docs_no_text += 1
                note = "no_text"      # in OCR mode: OCR recovered nothing usable
                mark(doc_id, "no_text")
                continue

            wave.append(_DocWork(
                entry=entry, chunks=chunks, meeting_date=meeting_date,
                doc_type=doc_type, page_count=extracted.page_count,
                text_chars=extracted.content_chars, document_id=doc_id,
            ))
            if sum(len(w.chunks) for w in wave) >= wave_size:
                await flush()
        finally:
            if on_doc is not None:
                on_doc(entry, note)

    await flush()
    return stats


# ---- reporting ---------------------------------------------------------

def render_report(
    stats: IngestStats, *, dry_run: bool, ocr: bool = False, tables: bool = False
) -> str:
    if tables:
        mode = "DRY RUN — nothing written" if dry_run else "written to database"
        lines = [
            "# Table backfill report",
            "",
            f"_{mode}_",
            "",
            "| docs seen | backfilled | no tables | not ingested | missing | errors "
            "| table chunks |",
            "|---|---|---|---|---|---|---|",
            f"| {stats.docs_seen} | {stats.docs_tables_backfilled} | {stats.docs_no_tables} "
            f"| {stats.docs_skipped} | {stats.docs_missing} | {stats.docs_error} "
            f"| {stats.chunks_written} |",
            "",
            "## Table chunks by district",
            "",
            "| district | table chunks |",
            "|---|---|",
        ]
        lines += [f"| {d} | {n} |" for d, n in stats.by_district.most_common()]
        return "\n".join(lines) + "\n"

    title = "OCR report" if ocr else "Ingest report"
    mode = "DRY RUN — nothing written" if dry_run else "written to database"
    lines = [
        f"# {title}",
        "",
        f"_{mode}_",
        "",
        "| docs seen | ingested | skipped | no text | missing | errors | chunks |",
        "|---|---|---|---|---|---|---|",
        f"| {stats.docs_seen} | {stats.docs_ingested} | {stats.docs_skipped} "
        f"| {stats.docs_no_text} | {stats.docs_missing} | {stats.docs_error} "
        f"| {stats.chunks_written} |",
        "",
    ]
    if ocr and dry_run:
        lines += [
            f"## OCR candidates by district — **{stats.docs_ocr_candidate} total**",
            "",
            "_(no-text documents a real run would OCR)_",
            "",
            "| district | candidates |",
            "|---|---|",
        ]
        lines += [f"| {d} | {n} |" for d, n in stats.ocr_candidates.most_common()]
        return "\n".join(lines) + "\n"
    lines += [
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


def _gather_pairs(
    *, root: str, manifest: str | None, district: str | None,
    doc_type: str | None, limit: int | None,
) -> tuple[list[tuple[ManifestEntry, Path]], int]:
    """Load manifest entries (with optional filters) from --root and --manifest."""
    explicit = [Path(m.strip()) for m in (manifest or "").split(",") if m.strip()]
    manifests = list(dict.fromkeys(explicit + find_manifests(root)))  # de-dupe, keep order
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
    return pairs, len(manifests)


@app.command("init-db")
def init_db(
    schema: str = typer.Option(
        "db/migrations/0001_schools_init.sql", help="Schema SQL file to apply."
    ),
) -> None:
    """Apply the schools schema to $SUPABASE_DB_URL (idempotent)."""
    from herald import schools_db

    sql = Path(schema).read_text(encoding="utf-8")
    # Raw connection: the schema creates the `vector` extension, so the
    # pgvector adapter can't be registered until *after* this runs.
    with schools_db.connect_raw(_db_url()) as conn:
        conn.execute(sql)
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
    pairs, _ = _gather_pairs(root=root, manifest=manifest, district=district,
                             doc_type=doc_type, limit=limit)

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


@app.command()
def tables(
    root: str = typer.Option(
        "data", help="Directory searched for **/manifest.jsonl (scrape artifacts)."
    ),
    manifest: str | None = typer.Option(
        None, help="Explicit manifest path(s), comma-separated; adds to --root's finds."
    ),
    district: str | None = typer.Option(None, help="Only backfill this district slug."),
    doc_type: str | None = typer.Option(None, help="Only backfill this doc type."),
    limit: int | None = typer.Option(None, help="Stop after N manifest entries."),
    dry_run: bool = typer.Option(
        True, help="Detect + count tables only; no DB, no Voyage."
    ),
    wave_size: int = typer.Option(DEFAULT_WAVE, help="Chunks per embed/write flush."),
    report: str | None = typer.Option(None, help="Write a markdown report here."),
) -> None:
    """Backfill whole-table (kind='table') chunks into already-ingested documents.

    For a corpus ingested before table-aware chunking existed: reprocess each
    *already-ingested* document, detect its tables, and add them as whole
    ``kind='table'`` chunks. Non-destructive — prose chunks, their embeddings,
    scores and cluster assignments are left alone, and re-running is safe
    (``on conflict do nothing``). The dry run (default) just counts how many
    tables each district would gain — fast, no Voyage, no writes.
    """
    pairs, _ = _gather_pairs(root=root, manifest=manifest, district=district,
                             doc_type=doc_type, limit=limit)

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
        if note in ("not-ingested", "no-tables"):
            if done % 100 == 0:
                console.print(f"[{done}/{len(pairs)}] scanning…")
            return
        console.print(f"[{done}/{len(pairs)}] {entry.district} {entry.title[:60]!r} {note}")

    async def go() -> IngestStats:
        try:
            return await ingest_manifests(
                pairs, conn=conn, voyage=voyage, wave_size=wave_size,
                on_doc=on_doc, tables_only=True,
            )
        finally:
            if voyage is not None:
                await voyage.aclose()

    try:
        stats = asyncio.run(go())
    finally:
        if conn is not None:
            conn.close()

    table = Table(title="Table backfill" + (" (dry run)" if dry_run else ""))
    for col in ("seen", "backfilled", "no_tables", "not_ingested", "missing",
                "errors", "table chunks"):
        table.add_column(col, justify="right")
    table.add_row(
        str(stats.docs_seen), str(stats.docs_tables_backfilled), str(stats.docs_no_tables),
        str(stats.docs_skipped), str(stats.docs_missing), str(stats.docs_error),
        str(stats.chunks_written),
    )
    console.print(table)
    for d, n in stats.by_district.most_common():
        console.print(f"  {d}: {n} table chunks")

    if report:
        Path(report).write_text(
            render_report(stats, dry_run=dry_run, tables=True), encoding="utf-8"
        )
        console.print(f"report: {report}")


@dataclass
class TableDBStats:
    seen: int = 0
    backfilled: int = 0      # docs that gained table chunks
    no_tables: int = 0       # fetched + parsed, but no detectable grid
    fetch_failed: int = 0    # URL unreachable / not a PDF
    chunks_written: int = 0
    by_district: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)  # per district


def _candidate_docs_sql(*, district: bool, only_missing: bool, limit: bool) -> str:
    """Ingested documents to (re)backfill tables for, newest-first per district.

    ``only_missing`` restricts to docs that don't already carry a ``kind='table'``
    chunk, so the command is idempotent — re-running only touches what's left.
    """
    where = ["d.ingest_status = 'ingested'"]
    if district:
        where.append("di.slug = %(district)s")
    if only_missing:
        # Two guards: the marker skips docs already processed (incl. those with
        # no tables, so they aren't re-fetched every run); the not-exists skips
        # docs that already carry table chunks (never append duplicates).
        where.append("d.tables_extracted_at is null")
        where.append(
            "not exists (select 1 from chunks c "
            "where c.document_id = d.id and c.kind = 'table')"
        )
    sql = (
        "select d.id, d.district_id, di.slug, d.source_url, d.doc_type, "
        "d.meeting_date, d.title "
        "from documents d join districts di on di.id = d.district_id "
        f"where {' and '.join(where)} "
        "order by di.slug, d.meeting_date desc nulls last"
    )
    if limit:
        sql += " limit %(limit)s"
    return sql


_BOARDDOCS_NSF = re.compile(r"(https?://go\.boarddocs\.com/[^/]+/[^/]+/Board\.nsf)/", re.I)


def _boarddocs_public_url(url: str) -> str | None:
    """The ``…/Board.nsf/Public`` page for a BoardDocs ``$file`` URL, else None.

    BoardDocs 403s a bare GET of a file URL; loading the district's /Public page
    once (on the same client, so its session cookie sticks) and sending a Referer
    is what the crawler does to get through — replicate it here.
    """
    m = _BOARDDOCS_NSF.match(url)
    return f"{m.group(1)}/Public" if m else None


def _tables_from_bytes(data: bytes) -> list[TableBlock]:
    """Write PDF bytes to a temp file and pull out its tables."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
        tf.write(data)
        tf.flush()
        return extract_pdf(tf.name).tables


def _fetch_pdf_tables(fetcher, source_url: str, *, primed: set[str]) -> list[TableBlock]:
    """Fetch a document's PDF over plain HTTP and pull out its tables.

    For BoardDocs URLs, prime the district session once (cached in ``primed``)
    and send a Referer. Raises on a non-PDF response or a transport error — the
    caller records the document as a fetch failure and moves on.
    """
    import contextlib

    headers: dict[str, str] = {}
    pub = _boarddocs_public_url(source_url)
    if pub:
        if pub not in primed:
            with contextlib.suppress(Exception):
                fetcher.get(pub)  # sets the BoardDocs session cookie on the client
            primed.add(pub)
        headers["Referer"] = pub

    resp = fetcher.get(source_url, headers=headers)
    data = resp.content
    ctype = resp.headers.get("content-type", "")
    if b"%PDF-" not in data[:1024] and "pdf" not in ctype.lower():
        raise ValueError(f"not a pdf (content-type={ctype!r})")
    return _tables_from_bytes(data)


async def _browser_pdf_tables(browser, source_url: str) -> list[TableBlock]:
    """Fetch a BoardDocs PDF through a real Chromium context (clears the WAF)."""
    pub = _boarddocs_public_url(source_url)
    if pub:
        await browser.prime(pub)
    data = await browser.get_bytes(source_url, referer=pub)
    if b"%PDF-" not in data[:1024]:
        raise ValueError("not a pdf (browser fetch)")
    return _tables_from_bytes(data)


@app.command("tables-db")
def tables_db(
    district: str | None = typer.Option(None, help="Only this district slug."),
    limit: int | None = typer.Option(None, help="Stop after N documents."),
    only_missing: bool = typer.Option(
        True, help="Only documents that don't already have table chunks."
    ),
    dry_run: bool = typer.Option(
        True, "--dry-run/--write",
        help="Count candidate documents only; no fetch, no writes.",
    ),
    min_interval: float = typer.Option(1.0, help="Seconds between fetches (politeness)."),
    use_browser: bool = typer.Option(
        True, "--browser/--no-browser",
        help="Fetch BoardDocs URLs through headless Chromium (clears the WAF 403).",
    ),
    wave_size: int = typer.Option(256, help="Table chunks per embed/write flush."),
    report: str | None = typer.Option(None, help="Write a markdown report here."),
) -> None:
    """Backfill whole-table chunks by re-fetching each ingested document from its
    stored ``source_url`` — no artifacts, no manifests, no content-hash matching.

    Iterates the documents already in the corpus, re-downloads each PDF directly
    from the URL it was ingested from, extracts its tables, and attaches them as
    ``kind='table'`` chunks to that existing document. Coverage is every ingested
    doc whose URL still resolves — not the fraction a fresh crawl happens to
    re-hash. Idempotent (``--only-missing`` skips docs already backfilled), so a
    re-run just picks up what failed last time.
    """
    from herald import schools_db
    from herald.scrape.core import BROWSER_HEADERS, BROWSER_USER_AGENT, Fetcher

    conn = schools_db.connect(_db_url())
    cur = conn.cursor()
    cur.execute(
        _candidate_docs_sql(district=bool(district), only_missing=only_missing,
                            limit=bool(limit)),
        {"district": district, "limit": limit},
    )
    docs = cur.fetchall()  # (id, district_id, slug, source_url, doc_type, date, title)
    scope = f" in {district}" if district else ""
    tail = " lacking tables" if only_missing else ""
    console.print(f"{len(docs)} candidate document(s){scope}{tail}")

    stats = TableDBStats()

    if dry_run:
        for slug, n in Counter(d[2] for d in docs).most_common():
            console.print(f"  {slug}: {n} docs")
        console.print("\n[yellow]dry run — no fetch, no writes[/yellow]")
        conn.close()
        if report:
            Path(report).write_text(_render_tables_db(stats, docs, dry_run=True),
                                    encoding="utf-8")
        return

    key = os.environ.get("VOYAGE_API_KEY", "")
    if not key:
        raise typer.BadParameter("VOYAGE_API_KEY is not set.")
    voyage = VoyageEmbedder(key)
    fetcher = Fetcher(
        user_agent=BROWSER_USER_AGENT, headers=BROWSER_HEADERS,
        min_request_interval=min_interval, respect_robots=False,
    )

    # Where each doc's existing chunk_index tops out, so table chunk_index
    # continues past it (the chunks PK is (document_id, chunk_index)).
    next_index: dict[object, int] = {}
    if docs:
        cur.execute(
            "select document_id, max(chunk_index) from chunks "
            "where document_id = any(%s) group by document_id",
            ([d[0] for d in docs],),
        )
        next_index = {r[0]: r[1] + 1 for r in cur.fetchall()}
    conn.commit()  # close the read transaction before the write waves

    primed: set[str] = set()          # BoardDocs /Public pages already loaded
    wave: list[tuple[object, object, list[Chunk]]] = []
    no_table_marks: list[object] = []  # doc ids fetched-but-tableless, to stamp done

    def _mark_done(ids: list[object]) -> None:
        if not ids:
            return
        with conn.transaction():
            conn.cursor().execute(
                "update documents set tables_extracted_at = now() where id = any(%s)",
                (ids,),
            )
        ids.clear()

    async def flush() -> None:
        if not wave:
            return
        all_chunks = [c for _, _, cs in wave for c in cs]
        vectors = await voyage.embed_documents([embed_input(c) for c in all_chunks])
        i = 0
        for doc_id, district_id, cs in wave:
            rows = []
            for c in cs:
                rows.append(schools_db.SchoolChunkRow(
                    chunk_index=c.order_index, section_path=c.section_path,
                    section_type=c.section_type, heading=c.heading, content=c.content,
                    embedding=vectors[i], meeting_date=c.meeting_date,
                    doc_type=c.doc_type, kind=c.kind,
                ))
                i += 1
            # Insert + stamp done atomically: a crash between the two would let a
            # re-run append the same tables again (chunk_index moves past them).
            with conn.transaction():
                cur2 = conn.cursor()
                schools_db.insert_chunks(cur2, document_id=doc_id,
                                         district_id=district_id, rows=rows)
                cur2.execute(
                    "update documents set tables_extracted_at = now() where id = %s",
                    (doc_id,),
                )
        wave.clear()

    async def go() -> None:
        browser = None
        if use_browser:
            from herald.browser_fetch import AsyncBrowserFetcher
            browser = AsyncBrowserFetcher(user_agent=BROWSER_USER_AGENT)
            try:
                await browser.start()
                console.print("[green]browser[/green] ready for BoardDocs fetches")
            except Exception as exc:
                console.print(f"[yellow]browser unavailable ({exc}); "
                              f"BoardDocs docs fall back to HTTP[/yellow]")
                browser = None
        try:
            for n, (doc_id, district_id, slug, url, doc_type, mdate, title) in enumerate(docs, 1):
                stats.seen += 1
                pub = _boarddocs_public_url(url)
                try:
                    if browser is not None and pub:
                        await asyncio.sleep(min_interval)  # politeness on BoardDocs
                        tables = await _browser_pdf_tables(browser, url)
                    else:
                        tables = _fetch_pdf_tables(fetcher, url, primed=primed)
                except Exception as exc:
                    stats.fetch_failed += 1
                    stats.failures[slug] += 1
                    console.print(f"[{n}/{len(docs)}] {slug} {title[:48]!r} "
                                  f"[red]fetch-failed[/red]: {str(exc)[:70]}")
                    continue
                start = next_index.get(doc_id, 0)
                chunks = _table_chunks(tables, start_order=start, district=slug,
                                       meeting_date=mdate, doc_type=doc_type, source_url=url)
                if not chunks:
                    # Fetched fine but no grids — stamp done so it isn't re-fetched.
                    stats.no_tables += 1
                    no_table_marks.append(doc_id)
                    if len(no_table_marks) >= 200:
                        _mark_done(no_table_marks)
                    continue
                wave.append((doc_id, district_id, chunks))
                stats.backfilled += 1
                stats.chunks_written += len(chunks)
                stats.by_district[slug] += len(chunks)
                console.print(f"[{n}/{len(docs)}] {slug} {title[:48]!r} {len(chunks)} table(s)")
                if sum(len(cs) for _, _, cs in wave) >= wave_size:
                    await flush()
            await flush()
            _mark_done(no_table_marks)
        finally:
            if browser is not None:
                await browser.close()

    try:
        asyncio.run(go())
    finally:
        fetcher.close()
        import contextlib
        with contextlib.suppress(Exception):
            asyncio.run(voyage.aclose())
        conn.close()

    table = Table(title="Table backfill (from source_url)")
    for col in ("seen", "backfilled", "no_tables", "fetch_failed", "table chunks"):
        table.add_column(col, justify="right")
    table.add_row(str(stats.seen), str(stats.backfilled), str(stats.no_tables),
                  str(stats.fetch_failed), str(stats.chunks_written))
    console.print(table)
    for d, n in stats.by_district.most_common():
        console.print(f"  {d}: {n} table chunks")
    if stats.failures:
        console.print("[red]fetch failures by district:[/red]")
        for d, n in stats.failures.most_common():
            console.print(f"  {d}: {n}")
    if report:
        Path(report).write_text(_render_tables_db(stats, docs, dry_run=False),
                                encoding="utf-8")
        console.print(f"report: {report}")


def _render_tables_db(stats: TableDBStats, docs: list, *, dry_run: bool) -> str:
    if dry_run:
        lines = [
            "# Table backfill (source_url) — candidates",
            "",
            f"_{len(docs)} ingested document(s) would be fetched — no writes_",
            "",
            "| district | candidate docs |",
            "|---|---|",
        ]
        lines += [f"| {s} | {n} |" for s, n in Counter(d[2] for d in docs).most_common()]
        return "\n".join(lines) + "\n"
    lines = [
        "# Table backfill (source_url)",
        "",
        "_written to database_",
        "",
        "| seen | backfilled | no tables | fetch failed | table chunks |",
        "|---|---|---|---|---|",
        f"| {stats.seen} | {stats.backfilled} | {stats.no_tables} "
        f"| {stats.fetch_failed} | {stats.chunks_written} |",
        "",
        "## Table chunks by district",
        "",
        "| district | table chunks |",
        "|---|---|",
    ]
    lines += [f"| {d} | {n} |" for d, n in stats.by_district.most_common()]
    return "\n".join(lines) + "\n"


@app.command()
def ocr(
    root: str = typer.Option(
        "data", help="Directory searched for **/manifest.jsonl (scrape artifacts)."
    ),
    manifest: str | None = typer.Option(
        None, help="Explicit manifest path(s), comma-separated; adds to --root's finds."
    ),
    district: str | None = typer.Option(None, help="Only OCR this district slug."),
    doc_type: str | None = typer.Option(None, help="Only OCR this doc type."),
    limit: int | None = typer.Option(None, help="Stop after N manifest entries."),
    dpi: int = typer.Option(300, help="Rasterization DPI for OCR."),
    max_pages: int | None = typer.Option(
        None, help="Cap pages OCR'd per document (None = all)."
    ),
    dry_run: bool = typer.Option(
        True, help="Count OCR candidates per district only; no OCR, no DB, no Voyage."
    ),
    wave_size: int = typer.Option(DEFAULT_WAVE, help="Chunks per embed/write flush."),
    report: str | None = typer.Option(None, help="Write a markdown report here."),
) -> None:
    """OCR the scanned (no-text) documents and add their chunks to the corpus.

    Reprocesses only documents that yielded no text on the normal ingest.
    The dry run (default) just tallies how many per district — fast, no
    Tesseract, no keys — so you can confirm the set before spending on OCR.
    """
    pairs, _ = _gather_pairs(root=root, manifest=manifest, district=district,
                             doc_type=doc_type, limit=limit)

    conn = None
    voyage = None
    ocr_fn = None
    if not dry_run:
        from herald import schools_db
        from herald.ocr import ocr_pdf

        conn = schools_db.connect(_db_url())
        key = os.environ.get("VOYAGE_API_KEY", "")
        if not key:
            raise typer.BadParameter("VOYAGE_API_KEY is not set.")
        voyage = VoyageEmbedder(key)

        def ocr_fn(path):
            return ocr_pdf(path, dpi=dpi, max_pages=max_pages)

    done = 0

    def on_doc(entry: ManifestEntry, note: str) -> None:
        nonlocal done
        done += 1
        # "has-text" is the overwhelming majority (already-ingested docs); stay quiet
        if note in ("ok", "skipped", "has-text"):
            if done % 100 == 0:
                console.print(f"[{done}/{len(pairs)}] scanning…")
            return
        console.print(f"[{done}/{len(pairs)}] {entry.district} {entry.title[:60]!r} {note}")

    async def go() -> IngestStats:
        try:
            return await ingest_manifests(
                pairs, conn=conn, voyage=voyage, wave_size=wave_size,
                on_doc=on_doc, ocr_mode=True, ocr_fn=ocr_fn,
            )
        finally:
            if voyage is not None:
                await voyage.aclose()

    try:
        stats = asyncio.run(go())
    finally:
        if conn is not None:
            conn.close()

    if dry_run:
        console.print(
            f"\n[bold]{stats.docs_ocr_candidate}[/bold] OCR candidate(s) "
            f"across {len(stats.ocr_candidates)} district(s):"
        )
        for d, n in stats.ocr_candidates.most_common():
            console.print(f"  {d}: {n}")
    else:
        table = Table(title="OCR")
        for col in ("seen", "recovered", "skipped", "still_empty", "missing",
                    "errors", "chunks"):
            table.add_column(col, justify="right")
        table.add_row(
            str(stats.docs_seen), str(stats.docs_ingested), str(stats.docs_skipped),
            str(stats.docs_no_text), str(stats.docs_missing), str(stats.docs_error),
            str(stats.chunks_written),
        )
        console.print(table)
        for d, n in stats.by_district.most_common():
            console.print(f"  {d}: {n} chunks recovered")

    if report:
        Path(report).write_text(
            render_report(stats, dry_run=dry_run, ocr=True), encoding="utf-8"
        )
        console.print(f"report: {report}")


if __name__ == "__main__":
    app()
