"""Topic-map clustering for the schools corpus.

Loads active chunk embeddings from the schools schema (``chunks``), projects
them to 2D with UMAP, groups them into discovered topics with HDBSCAN, labels
each topic with Haiku, and exports a compact JSON the interactive cluster map
renders (see docs/VIZ.md). One flat tier of topics — a hierarchy can come
later; the first map just answers "what themes are in the corpus, and which
districts sit where."

The UMAP/HDBSCAN parameters and the Haiku labelling loop mirror the inherited
newspaper ``cluster.py``; this module is the schools-schema, export-only cut
(no cluster tables written — the map reads a JSON artifact).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import typer
from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()

HAIKU_MODEL = "claude-haiku-4-5-20251001"
LABEL_CONCURRENCY = 5
REPS_PER_CLUSTER = 8
REP_CHARS = 320

LABEL_SYSTEM_PROMPT = """\
You will be given several short passages from Westchester County public
school-district governance documents (board agendas, minutes, policies,
handbooks, contracts, budgets), grouped by semantic similarity into one
cluster. The passages are a representative sample; the dominant theme should
be clear.

Identify the cluster's shared topic in 3 to 8 words. Be specific and concrete
— name the policy area, program, or document type (e.g. "Cell phone / device
policy", "Teacher CBA salary schedules", "Special education CSE/CPSE
placements", "Capital project bond bids"). Do not add commentary or quotation
marks.

If the passages are genuinely incoherent (garbled text, no shared topic),
reply with exactly: SKIP

Respond with ONLY the label or SKIP."""


@dataclass
class ClusterParams:
    min_cluster_size: int = 15
    min_samples: int = 5
    umap_neighbors: int = 15
    umap_min_dist: float = 0.1


@dataclass
class ChunkRow:
    chunk_id: str
    district: str
    meeting_date: _dt.date | None
    doc_type: str | None
    section_type: str | None
    heading: str | None
    content: str
    embedding: np.ndarray


# ---- loading -----------------------------------------------------------

def load_chunks(cur, *, sample: int | None = None) -> list[ChunkRow]:
    """Active, embedded chunks joined to their district slug.

    ``sample`` (if set) draws a random subset — a lighter map for a quick look
    or a phone-friendly artifact.
    """
    limit = "" if sample is None else "order by random() limit %(sample)s"
    cur.execute(
        f"""
        select c.id, d.slug, c.meeting_date, c.doc_type, c.section_type,
               c.heading, c.content, c.embedding
        from chunks c
        join districts d on d.id = c.district_id
        where c.status = 'active' and c.embedding is not null
        {limit}
        """,
        {"sample": sample},
    )
    out: list[ChunkRow] = []
    for r in cur.fetchall():
        out.append(ChunkRow(
            chunk_id=str(r[0]), district=r[1], meeting_date=r[2], doc_type=r[3],
            section_type=r[4], heading=r[5], content=r[6],
            embedding=np.asarray(r[7], dtype=np.float32),
        ))
    return out


# ---- projection + clustering ------------------------------------------

def hdbscan_labels(embeddings: np.ndarray, params: ClusterParams) -> np.ndarray:
    import hdbscan

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=params.min_cluster_size,
        min_samples=params.min_samples,
        metric="euclidean",
        core_dist_n_jobs=-1,
        cluster_selection_method="leaf",
    )
    return clusterer.fit_predict(embeddings)


def umap_project(embeddings: np.ndarray, params: ClusterParams) -> np.ndarray:
    import umap

    reducer = umap.UMAP(
        n_components=2, n_neighbors=params.umap_neighbors,
        min_dist=params.umap_min_dist, metric="cosine",
        random_state=42, low_memory=True,
    )
    xy = np.asarray(reducer.fit_transform(embeddings), dtype=np.float32)
    for dim in range(2):  # normalize each axis to [0, 1] for the renderer
        col = xy[:, dim]
        lo, hi = float(col.min()), float(col.max())
        xy[:, dim] = (col - lo) / (hi - lo) if hi - lo > 1e-10 else 0.5
    return xy


def representative_indices(
    embeddings: np.ndarray, labels: np.ndarray, *, per_cluster: int = REPS_PER_CLUSTER
) -> dict[int, list[int]]:
    """Per cluster, the ``per_cluster`` chunks nearest the cluster centroid."""
    reps: dict[int, list[int]] = {}
    for lab in sorted({int(x) for x in labels if x >= 0}):
        idx = np.where(labels == lab)[0]
        centroid = embeddings[idx].mean(axis=0)
        # cosine distance to centroid; smallest = most representative
        vecs = embeddings[idx]
        sims = vecs @ centroid / (
            np.linalg.norm(vecs, axis=1) * np.linalg.norm(centroid) + 1e-9
        )
        order = idx[np.argsort(-sims)][:per_cluster]
        reps[lab] = [int(i) for i in order]
    return reps


# ---- labelling ---------------------------------------------------------

async def label_clusters(
    api_key: str, reps: dict[int, list[str]]
) -> dict[int, str]:
    """Haiku label per cluster from its representative passages."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0)
    sem = asyncio.Semaphore(LABEL_CONCURRENCY)
    labels: dict[int, str] = {}

    async def one(lab: int, passages: list[str]) -> None:
        async with sem:
            user = "\n\n---\n\n".join(p[:REP_CHARS] for p in passages)
            try:
                msg = await client.messages.create(
                    model=HAIKU_MODEL, max_tokens=40, temperature=0,
                    system=LABEL_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(getattr(b, "text", "") for b in msg.content).strip()
                first = text.splitlines()[0].strip() if text else ""
                if first and first.upper().strip(".") != "SKIP":
                    labels[lab] = first[:120]
            except Exception as exc:  # a failed label shouldn't kill the run
                logger.warning("label cluster %d failed: %s", lab, exc)

    try:
        await asyncio.gather(*(one(lab, ps) for lab, ps in reps.items()))
    finally:
        await client.aclose()
    return labels


# ---- export ------------------------------------------------------------

def _tooltip(row: ChunkRow) -> str:
    bits = [b for b in (row.section_type, row.heading) if b]
    text = " — ".join(dict.fromkeys(bits)) or row.content[:80]
    return text[:100]


def build_export(
    rows: list[ChunkRow], labels: np.ndarray, xy: np.ndarray,
    cluster_labels: dict[int, str],
) -> dict:
    """Compact, columnar JSON for the renderer (arrays, not per-point objects)."""
    districts = sorted({r.district for r in rows})
    doc_types = sorted({r.doc_type or "other" for r in rows})
    d_idx = {s: i for i, s in enumerate(districts)}
    t_idx = {s: i for i, s in enumerate(doc_types)}

    sizes: dict[int, int] = defaultdict(int)
    for lab in labels:
        sizes[int(lab)] += 1
    clusters = [
        {"id": lab, "label": cluster_labels.get(lab, f"Topic {lab}"), "size": sizes[lab]}
        for lab in sorted(sizes)
        if lab >= 0
    ]

    return {
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "n_points": len(rows),
        "n_clusters": len(clusters),
        "n_noise": sizes.get(-1, 0),
        "districts": districts,
        "doc_types": doc_types,
        "clusters": clusters,
        "x": [round(float(v), 4) for v in xy[:, 0]],
        "y": [round(float(v), 4) for v in xy[:, 1]],
        "cluster": [int(v) for v in labels],
        "district": [d_idx[r.district] for r in rows],
        "doc_type": [t_idx[r.doc_type or "other"] for r in rows],
        "month": [r.meeting_date.strftime("%Y-%m") if r.meeting_date else "" for r in rows],
        "tip": [_tooltip(r) for r in rows],
    }


# ---- orchestration -----------------------------------------------------

def run_clustering(
    rows: list[ChunkRow], params: ClusterParams, *, api_key: str | None,
    on_progress=lambda s: None,
) -> dict:
    embeddings = np.vstack([r.embedding for r in rows]).astype(np.float32)
    on_progress(f"HDBSCAN over {len(rows)} chunks (min_cluster_size={params.min_cluster_size})")
    labels = hdbscan_labels(embeddings, params)
    n_clusters = len({int(x) for x in labels if x >= 0})
    on_progress(f"  {n_clusters} topics, {int((labels < 0).sum())} noise points")
    on_progress("UMAP projection to 2D")
    xy = umap_project(embeddings, params)

    cluster_labels: dict[int, str] = {}
    if api_key and n_clusters:
        on_progress(f"labelling {n_clusters} topics with Haiku")
        rep_idx = representative_indices(embeddings, labels)
        reps = {lab: [rows[i].content for i in idxs] for lab, idxs in rep_idx.items()}
        cluster_labels = asyncio.run(label_clusters(api_key, reps))
        on_progress(f"  {len(cluster_labels)}/{n_clusters} labelled")
    return build_export(rows, labels, xy, cluster_labels)


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Cluster the schools corpus into a topic map.", no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Group callback so ``run`` stays a named subcommand (room to grow)."""


@app.command()
def run(
    out: str = typer.Option("cluster-map.json", help="Output JSON path."),
    sample: int | None = typer.Option(
        None, help="Random-sample this many chunks (lighter map; default: all)."
    ),
    min_cluster_size: int = typer.Option(15, help="HDBSCAN min_cluster_size."),
    min_samples: int = typer.Option(5, help="HDBSCAN min_samples."),
    umap_neighbors: int = typer.Option(15, help="UMAP n_neighbors."),
    no_labels: bool = typer.Option(
        False, "--no-labels", help="Skip Haiku labelling (no ANTHROPIC_API_KEY needed)."
    ),
) -> None:
    """Load embeddings → UMAP + HDBSCAN → Haiku labels → JSON for the map."""
    from herald import schools_db

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    api_key = None if no_labels else os.environ.get("ANTHROPIC_API_KEY") or None
    if not no_labels and not api_key:
        console.print("[yellow]ANTHROPIC_API_KEY not set — topics will be unlabelled[/yellow]")

    params = ClusterParams(
        min_cluster_size=min_cluster_size, min_samples=min_samples,
        umap_neighbors=umap_neighbors,
    )
    with schools_db.connect(db_url) as conn:
        rows = load_chunks(conn.cursor(), sample=sample)
    console.print(f"loaded {len(rows)} chunks")
    if not rows:
        raise typer.Exit(1)

    export = run_clustering(rows, params, api_key=api_key,
                            on_progress=lambda s: console.print(s))
    Path(out).write_text(json.dumps(export, separators=(",", ":")), encoding="utf-8")
    size_kb = Path(out).stat().st_size / 1024
    console.print(
        f"[green]wrote[/green] {out} — {export['n_clusters']} topics, "
        f"{export['n_points']} points ({size_kb:.0f} KB)"
    )


if __name__ == "__main__":
    app()
