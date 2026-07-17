# ============================================================
# FORK TODO (herald): CLI: rework commands (ingest --lccn/--from/--to -> --district/--year)
# Inherited from Herald; rewrite for this corpus before use.
# See docs/ENGINE_EXTRACTION.md.
# ============================================================
"""Herald CLI.

Phase 1 surface:
- newspaper ``ingest`` removed - schools ingest lives in ``herald-ingest`` (herald.ingest_schools)
- ``herald ask "<question>"``  (stub — wired in a later slice)
- ``herald normalize-text <path>`` (debug helper)
"""

from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console

from herald import db, normalize, settings
from herald.cluster import ClusterParams, run_labels_only, run_pipeline
from herald.embed import VoyageEmbedder
from herald.eval import (
    EVAL_QUESTIONS,
    format_results_markdown,
    run_eval,
)
from herald.rerank import VoyageReranker
from herald.retrieval import HybridRetriever
from herald.synth import Synthesizer

app = typer.Typer(
    add_completion=False,
    help="Semantic research over historical American newspapers.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural-language question"),
    date_from: str | None = typer.Option(None, "--from", help="Earliest issue date (YYYY-MM-DD)"),
    date_to: str | None = typer.Option(None, "--to", help="Latest issue date (YYYY-MM-DD)"),
    final_top: int = typer.Option(12, "--top", help="Number of chunks to return"),
    no_rerank: bool = typer.Option(
        False,
        "--no-rerank",
        help="Skip Voyage rerank-2.5 and return RRF order directly.",
    ),
) -> None:
    """Retrieve passages relevant to a question (no LLM synthesis yet)."""
    cfg = settings.load()
    df = _parse_date(date_from) if date_from else None
    dt = _parse_date(date_to) if date_to else None
    asyncio.run(_ask(cfg, question, df, dt, final_top, no_rerank))


async def _ask(
    cfg: settings.Settings,
    question: str,
    df: date | None,
    dt: date | None,
    final_top: int,
    no_rerank: bool,
) -> None:
    if not cfg.supabase_db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    if not cfg.voyage_api_key:
        raise typer.BadParameter("VOYAGE_API_KEY is not set.")

    conn = db.connect(cfg.supabase_db_url)
    try:
        async with (
            VoyageEmbedder(api_key=cfg.voyage_api_key) as embedder,
            VoyageReranker(api_key=cfg.voyage_api_key) as reranker,
        ):
            retriever = HybridRetriever(
                conn=conn,
                embedder=embedder,
                reranker=None if no_rerank else reranker,
            )
            hits = await retriever.retrieve(
                question,
                date_from=df,
                date_to=dt,
                final_top=final_top,
                rerank=not no_rerank,
            )
    finally:
        conn.close()

    console.print(f"\n[bold]Q:[/bold] {question}")
    window_bits = []
    if df:
        window_bits.append(f"from={df}")
    if dt:
        window_bits.append(f"to={dt}")
    if window_bits:
        console.print(f"  ({' '.join(window_bits)})")

    if not hits:
        console.print("\n[yellow]No matching chunks.[/yellow]")
        raise typer.Exit(code=0)

    console.print(
        f"\n[bold]{len(hits)} chunks[/bold]  "
        f"(rerank={'off' if no_rerank else 'rerank-2.5'})\n"
    )
    for i, h in enumerate(hits, start=1):
        snippet = h.content.strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        score_label = "rerank" if h.rerank_score is not None else "rrf"
        console.print(
            f"[bold cyan][{i}][/bold cyan] "
            f"{score_label}={h.score:.4f}  "
            f"[white]{h.paper_title}[/white], "
            f"{h.date_issued}, p.{h.page_sequence}"
        )
        console.print(f"    {snippet}")
        console.print(f"    [dim]{h.image_url}[/dim]\n")


@app.command()
def answer(
    question: str = typer.Argument(..., help="Natural-language question"),
    date_from: str | None = typer.Option(None, "--from", help="Earliest issue date (YYYY-MM-DD)"),
    date_to: str | None = typer.Option(None, "--to", help="Latest issue date (YYYY-MM-DD)"),
    final_top: int = typer.Option(12, "--top", help="Chunks to send to Claude"),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Skip Voyage rerank"),
) -> None:
    """Retrieve passages and synthesize a cited answer via Claude Sonnet 4.6."""
    cfg = settings.load()
    df = _parse_date(date_from) if date_from else None
    dt = _parse_date(date_to) if date_to else None
    asyncio.run(_answer(cfg, question, df, dt, final_top, no_rerank))


async def _answer(
    cfg: settings.Settings,
    question: str,
    df: date | None,
    dt: date | None,
    final_top: int,
    no_rerank: bool,
) -> None:
    if not cfg.supabase_db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    if not cfg.voyage_api_key:
        raise typer.BadParameter("VOYAGE_API_KEY is not set.")
    if not cfg.anthropic_api_key:
        raise typer.BadParameter(
            "ANTHROPIC_API_KEY is not set. Get one at console.anthropic.com."
        )

    conn = db.connect(cfg.supabase_db_url)
    try:
        async with (
            VoyageEmbedder(api_key=cfg.voyage_api_key) as embedder,
            VoyageReranker(api_key=cfg.voyage_api_key) as reranker,
        ):
            retriever = HybridRetriever(
                conn=conn,
                embedder=embedder,
                reranker=None if no_rerank else reranker,
            )
            hits = await retriever.retrieve(
                question,
                date_from=df,
                date_to=dt,
                final_top=final_top,
                rerank=not no_rerank,
            )
        synth = Synthesizer(api_key=cfg.anthropic_api_key)
        result = await synth.answer(question, hits)
    finally:
        conn.close()

    console.print(f"\n[bold]Q:[/bold] {question}\n")
    console.print(result.text)
    console.print()
    if result.cited_indices:
        console.print(f"[bold]Sources cited[/bold] ({len(set(result.cited_indices))} unique):")
        seen: set[int] = set()
        for n in result.cited_indices:
            if n in seen:
                continue
            seen.add(n)
            h = hits[n - 1]
            console.print(
                f"  [bold cyan][{n}][/bold cyan]  {h.paper_title}, "
                f"{h.date_issued}, p.{h.page_sequence}"
            )
            console.print(f"        [dim]{h.image_url}[/dim]")
    else:
        console.print("[yellow]No inline citations in answer.[/yellow]")
    console.print(
        f"\n[dim]tokens: in={result.input_tokens} out={result.output_tokens}  "
        f"refusal={result.refused}[/dim]"
    )


@app.command(name="eval")
def eval_cmd(
    question: int | None = typer.Option(
        None, "--question", "-q",
        help="Run only question N (1..10). Default: all 10.",
    ),
    output: str | None = typer.Option(
        None, "--output", "-o",
        help="Write Markdown report to this path. Default: print to stdout.",
    ),
    date_from: str | None = typer.Option(None, "--from", help="Earliest issue date"),
    date_to: str | None = typer.Option(None, "--to", help="Latest issue date"),
    final_top: int = typer.Option(12, "--top", help="Chunks per question"),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Skip Voyage rerank"),
) -> None:
    """Run the 10 Phase 1 validation questions (PLAN §12).

    The output is a Markdown report sized for hand-grading: each
    question gets its answer, the citation list with image URLs, and
    token-usage stats.
    """
    cfg = settings.load()
    df = _parse_date(date_from) if date_from else None
    dt = _parse_date(date_to) if date_to else None

    if question is not None:
        matching = [q for q in EVAL_QUESTIONS if q.number == question]
        if not matching:
            raise typer.BadParameter(
                f"Question {question} not in 1..{len(EVAL_QUESTIONS)}"
            )
        chosen = matching
    else:
        chosen = list(EVAL_QUESTIONS)

    asyncio.run(_eval(cfg, chosen, df, dt, final_top, no_rerank, output))


async def _eval(
    cfg: settings.Settings,
    chosen: list,
    df: date | None,
    dt: date | None,
    final_top: int,
    no_rerank: bool,
    output: str | None,
) -> None:
    if not cfg.supabase_db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    if not cfg.voyage_api_key:
        raise typer.BadParameter("VOYAGE_API_KEY is not set.")
    if not cfg.anthropic_api_key:
        raise typer.BadParameter(
            "ANTHROPIC_API_KEY is not set. Get one at console.anthropic.com."
        )

    conn = db.connect(cfg.supabase_db_url)
    try:
        async with (
            VoyageEmbedder(api_key=cfg.voyage_api_key) as embedder,
            VoyageReranker(api_key=cfg.voyage_api_key) as reranker,
        ):
            retriever = HybridRetriever(
                conn=conn,
                embedder=embedder,
                reranker=None if no_rerank else reranker,
            )
            synth = Synthesizer(api_key=cfg.anthropic_api_key)
            results = await run_eval(
                retriever=retriever,
                synthesizer=synth,
                questions=chosen,
                date_from=df,
                date_to=dt,
                final_top=final_top,
                rerank=not no_rerank,
                on_progress=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
            )
    finally:
        conn.close()

    md = format_results_markdown(
        results, date_from=df, date_to=dt,
    )
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(md)
        console.print(f"\n[bold green]wrote eval report[/bold green]  {output}")
        console.print(f"  questions={len(results)}  "
                      f"total_in={sum(r.answer.input_tokens for r in results)}  "
                      f"total_out={sum(r.answer.output_tokens for r in results)}")
    else:
        # Print straight to stdout — markdown is plenty readable.
        # Don't pipe through rich.console (it'll mangle markdown formatting
        # heuristically); use plain print so the user can pipe / redirect.
        print(md)


@app.command()
def cluster(
    min_cluster_size: int = typer.Option(15, help="HDBSCAN min_cluster_size"),
    min_samples: int = typer.Option(5, help="HDBSCAN min_samples"),
    umap_neighbors: int = typer.Option(15, help="UMAP n_neighbors"),
    umap_min_dist: float = typer.Option(0.1, help="UMAP min_dist"),
    tier1: int = typer.Option(50, help="Target cluster count for tier 1"),
    tier2: int = typer.Option(15, help="Target cluster count for tier 2"),
    tier3: int = typer.Option(5, help="Target cluster count for tier 3"),
) -> None:
    """Compute clusters, UMAP projections, and content classifications."""
    cfg = settings.load()
    if not cfg.supabase_db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")

    params = ClusterParams(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        umap_neighbors=umap_neighbors,
        umap_min_dist=umap_min_dist,
        tier1_target=tier1,
        tier2_target=tier2,
        tier3_target=tier3,
    )

    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Clustering...", total=None)

        def on_progress(msg: str) -> None:
            progress.update(task, description=msg)

        result = run_pipeline(cfg.supabase_db_url, params, on_progress)

    console.print("\n[bold green]Clustering complete[/bold green]")
    console.print(f"  run_id: {result.run_id}")
    console.print(f"  chunks: {result.chunk_count}")
    console.print(f"  outliers: {result.outlier_count}")
    for tier in sorted(result.tier_counts):
        console.print(f"  tier {tier}: {result.tier_counts[tier]} clusters")
    console.print(f"  labels generated: {result.labels_generated}")
    from herald.classify import LABELS
    for t, count in sorted(result.content_type_counts.items()):
        console.print(f"  {LABELS.get(t, f'type_{t}')}: {count}")


@app.command()
def relabel() -> None:
    """Regenerate Haiku cluster labels for the active cluster run.

    Skips clustering entirely — just reads the existing run and
    writes labels to clusters.label_text. Useful when labels failed
    previously (e.g. missing column) or you tweaked the prompt.
    """
    cfg = settings.load()
    if not cfg.supabase_db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    if not cfg.anthropic_api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set.")

    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Relabeling...", total=None)

        def on_progress(msg: str) -> None:
            progress.update(task, description=msg)

        written = run_labels_only(cfg.supabase_db_url, on_progress)

    console.print(f"\n[bold green]Relabel complete[/bold green]: {written} labels written")


@app.command()
def normalize_text(
    path: str = typer.Argument(..., help="Path to a raw OCR .txt file"),
) -> None:
    """Print the normalized form of a raw OCR text file (debugging helper)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    console.print(normalize.normalize_ocr(raw))


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise typer.BadParameter(f"date must be YYYY-MM-DD: {s}") from e
