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
import contextlib
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
    cluster_dims: int = 10       # UMAP target dim for HDBSCAN (not 2 — 2D over-merges)


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


def umap_reduce(embeddings: np.ndarray, params: ClusterParams) -> np.ndarray:
    """Reduce to ``cluster_dims`` for HDBSCAN — the clustering space.

    Straight to 2D over-merges topics (the 2D layout optimizes visual
    separation, not density); the raw 1024-D drowns points in noise. A
    mid-dimensional cosine UMAP (``min_dist=0`` packs points for density
    clustering) is the standard middle ground.
    """
    import umap

    reducer = umap.UMAP(
        n_components=params.cluster_dims, n_neighbors=params.umap_neighbors,
        min_dist=0.0, metric="cosine", random_state=42, low_memory=True,
    )
    return np.asarray(reducer.fit_transform(embeddings), dtype=np.float32)


def _normalize01(xy: np.ndarray) -> np.ndarray:
    """Scale each axis to [0, 1] for the renderer (preserving aspect roughly)."""
    xy = np.asarray(xy, dtype=np.float32).copy()
    for dim in range(xy.shape[1]):
        col = xy[:, dim]
        lo, hi = float(col.min()), float(col.max())
        xy[:, dim] = (col - lo) / (hi - lo) if hi - lo > 1e-10 else 0.5
    return xy


def project_chunks(embeddings: np.ndarray, params: ClusterParams) -> np.ndarray:
    """Lay *every chunk* out in 2D — a cosine UMAP straight on the embeddings.

    This mirrors the newspaper map (one dot per chunk): with a direct cosine
    projection, a topic's chunks co-locate into a visual "bubble", and — the
    property worth keeping — *semantic neighborhoods are real*, so a keyword's
    matches sit together on the map even when they span several clusters. The
    earlier chained projection (euclidean UMAP of the already-reduced 10-D
    space) destroyed that locality and scattered topics across the plane.
    """
    import umap

    reducer = umap.UMAP(
        n_components=2, n_neighbors=params.umap_neighbors,
        min_dist=params.umap_min_dist, metric="cosine",
        random_state=42, low_memory=True,
    )
    return _normalize01(reducer.fit_transform(embeddings))


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


# ---- hierarchy (cluster-of-clusters) -----------------------------------

def leaf_centroids(embeddings: np.ndarray, labels: np.ndarray) -> tuple[list[int], np.ndarray]:
    """L2-normalized mean embedding per leaf cluster (noise excluded)."""
    ids = sorted({int(x) for x in labels if x >= 0})
    cents = []
    for lab in ids:
        v = embeddings[labels == lab].mean(axis=0)
        n = float(np.linalg.norm(v))
        cents.append(v / n if n > 1e-9 else v)
    return ids, np.asarray(cents, dtype=np.float32)


def build_hierarchy(
    leaf_ids: list[int], centroids: np.ndarray, targets: list[int]
) -> list[dict]:
    """Merge leaf centroids into coarser tiers — a *guaranteed-nesting* hierarchy.

    One agglomerative linkage over the leaf centroids, then cut it at each
    ``target`` cluster count. Because every tier is a cut of the *same* tree,
    a leaf's ancestors strictly nest (unlike re-running HDBSCAN per level,
    where the partitions need not agree). Tiers coarser than the leaf set
    only (``target >= n_leaves`` is the leaf tier itself, so it's dropped);
    smallest target = level 0 (broadest themes).

    Uses **Ward** linkage: ``average``/``complete`` linkage in high-D chains
    badly — it peels outlier leaves off one at a time (a long tail of
    singleton "themes") while dumping the dense mass into a few giant blobs.
    Ward minimizes within-tier variance, giving balanced, browsable themes.
    The centroids are L2-normalized, so squared Euclidean distance is an affine
    function of cosine (2 - 2*cos) — Ward's required Euclidean metric therefore
    clusters in cosine space.
    """
    from scipy.cluster.hierarchy import fcluster, linkage

    n = len(leaf_ids)
    if n < 3:
        return []
    z = linkage(centroids, method="ward", metric="euclidean")
    tiers: list[dict] = []
    for k in sorted({int(t) for t in targets}):
        if k < 2 or k >= n:          # coarser-than-leaves only
            continue
        assign = fcluster(z, t=k, criterion="maxclust")
        remap = {c: i for i, c in enumerate(sorted(set(assign)))}
        groups: dict[int, list[int]] = defaultdict(list)
        for leaf_pos, c in enumerate(assign):
            groups[remap[c]].append(leaf_ids[leaf_pos])
        tiers.append({"target": k, "groups": dict(groups)})
    return tiers


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
        # a teardown error must never discard labels we already computed
        with contextlib.suppress(Exception):
            await client.close()
    return labels


async def label_hierarchy(
    api_key: str, tiers: list[dict], leaf_reps: dict[int, list[str]]
) -> list[dict[int, str]]:
    """Haiku-label each hierarchy tier, pooling passages from its child leaves.

    Draws *one* passage from as many distinct child leaves as possible (up to
    16) rather than several from a few — a balanced theme spans ~40 leaves, so
    a broad cross-section names the umbrella better than a deep sample of two.
    """
    out: list[dict[int, str]] = []
    for tier in tiers:
        reps: dict[int, list[str]] = {}
        for pid, leaves in tier["groups"].items():
            pooled: list[str] = []
            for lid in leaves:                       # one per leaf, widest coverage
                got = leaf_reps.get(lid, [])
                if got:
                    pooled.append(got[0])
                if len(pooled) >= 16:
                    break
            reps[pid] = pooled
        out.append(await label_clusters(api_key, reps))
    return out


# ---- export ------------------------------------------------------------

def _tooltip(row: ChunkRow) -> str:
    bits = [b for b in (row.section_type, row.heading) if b]
    text = " — ".join(dict.fromkeys(bits)) or row.content[:80]
    return text[:100]


def build_export(
    rows: list[ChunkRow], labels: np.ndarray, chunk_xy: np.ndarray,
    leaf_ids: list[int], cluster_labels: dict[int, str],
    hierarchy: list[dict] | None = None,
    hierarchy_labels: list[dict[int, str]] | None = None,
    rep_idx: dict[int, list[int]] | None = None,
) -> dict:
    """Columnar JSON for the map — **one point per chunk** (like the newspaper).

    The parallel arrays (``x``/``y``/``cluster``/``district``/``month``) carry
    every chunk; the cosine projection keeps semantic neighborhoods intact, so a
    topic's points co-locate and a keyword's matches sit together on the map
    even across cluster lines. ``clusters`` (leaf topics, with ``theme``/``mid``
    parents) and ``hierarchy`` let the renderer recolor the *same* points at
    Fine / Medium / Broad levels and drive the drill-down legend.
    """
    districts = sorted({r.district for r in rows})
    doc_types = sorted({r.doc_type or "other" for r in rows})
    d_idx = {s: i for i, s in enumerate(districts)}
    rep_idx = rep_idx or {}

    # leaf -> hierarchy parents (tier 0 = theme, tier 1 = mid)
    theme_of: dict[int, int] = {}
    mid_of: dict[int, int] = {}
    if hierarchy:
        if len(hierarchy) >= 1:
            for pid, leaves in hierarchy[0]["groups"].items():
                for leaf in leaves:
                    theme_of[leaf] = pid
        if len(hierarchy) >= 2:
            for pid, leaves in hierarchy[1]["groups"].items():
                for leaf in leaves:
                    mid_of[leaf] = pid

    sizes: dict[int, int] = defaultdict(int)
    for lab in labels:
        sizes[int(lab)] += 1

    clusters = [
        {
            "id": lid,
            "label": cluster_labels.get(lid, f"Topic {lid}"),
            "size": sizes[lid],
            "theme": theme_of.get(lid, -1),
            "mid": mid_of.get(lid, -1),
            "tip": _tooltip(rows[(rep_idx.get(lid) or [0])[0]]) if rep_idx.get(lid) else "",
        }
        for lid in leaf_ids
    ]

    out = {
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "n_points": len(rows),
        "n_clusters": len(clusters),
        "n_noise": sizes.get(-1, 0),
        "districts": districts,
        "doc_types": doc_types,
        "clusters": clusters,
        "x": [round(float(v), 4) for v in chunk_xy[:, 0]],
        "y": [round(float(v), 4) for v in chunk_xy[:, 1]],
        "cluster": [int(v) for v in labels],
        "district": [d_idx[r.district] for r in rows],
        "month": [r.meeting_date.strftime("%Y-%m") if r.meeting_date else "" for r in rows],
    }

    if hierarchy:
        labels_per_tier = hierarchy_labels or [{} for _ in hierarchy]
        tiers_out = []
        for level, (tier, hl) in enumerate(zip(hierarchy, labels_per_tier, strict=False)):
            tclusters = [
                {
                    "id": pid,
                    "label": hl.get(pid, f"Group {pid}"),
                    "size": sum(sizes[leaf] for leaf in leaves),
                    "leaves": sorted(leaves),
                }
                for pid, leaves in sorted(tier["groups"].items())
            ]
            tiers_out.append({"level": level, "target": tier["target"], "clusters": tclusters})
        out["hierarchy"] = tiers_out

    return out


# ---- content embeddings ------------------------------------------------

def content_embeddings(voyage_key: str, rows: list[ChunkRow], *, on_progress) -> np.ndarray:
    """Re-embed each chunk's *raw content* (no district/date prefix).

    The stored ``chunks.embedding`` carries the contextual prefix
    ("{district} · {date} · …"), which is right for retrieval but makes a
    topic map cluster by district. Embedding content alone yields a topic
    vector. (Voyage batches internally; ~$0.35 for the full corpus.)
    """
    from herald.embed import VoyageEmbedder

    on_progress(f"re-embedding {len(rows)} chunks content-only via Voyage")

    async def go() -> list[list[float]]:
        async with VoyageEmbedder(voyage_key) as v:
            return await v.embed_documents([r.content for r in rows])

    return np.asarray(asyncio.run(go()), dtype=np.float32)


# ---- orchestration -----------------------------------------------------

def run_clustering(
    rows: list[ChunkRow], params: ClusterParams, *, embeddings: np.ndarray | None = None,
    api_key: str | None = None, hierarchy_targets: list[int] | None = None,
    on_progress=lambda s: None,
) -> dict:
    """Cluster ``rows`` and build the per-chunk map export.

    UMAP-reduce to ``cluster_dims`` → HDBSCAN there → **project every chunk** to
    2D with a direct cosine UMAP (preserves semantic locality). ``embeddings``
    overrides ``rows``' stored vectors (e.g. content-only). ``hierarchy_targets``
    (e.g. ``[15, 60]``) merges the leaf centroids into coarser tiers, which the
    map uses to recolor the points at Fine / Medium / Broad levels.
    """
    if embeddings is None:
        embeddings = np.vstack([r.embedding for r in rows]).astype(np.float32)
    on_progress(f"UMAP → {params.cluster_dims}D over {len(rows)} chunks (clustering space)")
    reduced = umap_reduce(embeddings, params)
    on_progress(f"HDBSCAN on {params.cluster_dims}D (min_cluster_size={params.min_cluster_size}, "
                f"min_samples={params.min_samples})")
    labels = hdbscan_labels(reduced, params)
    n_clusters = len({int(x) for x in labels if x >= 0})
    n_noise = int((labels < 0).sum())
    pct = 100 * n_noise / max(len(rows), 1)
    on_progress(f"  {n_clusters} topics, {n_noise} noise ({pct:.0f}%)")

    on_progress(f"projecting {len(rows)} chunks → 2D (cosine, direct)")
    chunk_xy = project_chunks(embeddings, params)
    leaf_ids, cents = leaf_centroids(embeddings, labels)
    rep_idx = representative_indices(embeddings, labels) if n_clusters else {}
    cluster_labels: dict[int, str] = {}
    if api_key and n_clusters:
        on_progress(f"labelling {n_clusters} topics with Haiku")
        reps = {lab: [rows[i].content for i in idxs] for lab, idxs in rep_idx.items()}
        cluster_labels = asyncio.run(label_clusters(api_key, reps))
        on_progress(f"  {len(cluster_labels)}/{n_clusters} labelled")

    hierarchy = hierarchy_labels = None
    if hierarchy_targets and n_clusters >= 3:
        on_progress(f"building hierarchy (merge {n_clusters} leaf centroids → {hierarchy_targets})")
        hierarchy = build_hierarchy(leaf_ids, cents, hierarchy_targets)
        on_progress(f"  {len(hierarchy)} tier(s): " +
                    ", ".join(str(len(t['groups'])) for t in hierarchy))
        if api_key and hierarchy:
            leaf_reps = {lab: [rows[i].content for i in idxs] for lab, idxs in rep_idx.items()}
            hierarchy_labels = asyncio.run(label_hierarchy(api_key, hierarchy, leaf_reps))
    return build_export(rows, labels, chunk_xy, leaf_ids, cluster_labels,
                        hierarchy, hierarchy_labels, rep_idx=rep_idx)


# ---- parameter sweep ---------------------------------------------------

@dataclass
class SweepResult:
    cluster_dims: int
    min_cluster_size: int
    n_clusters: int
    noise_pct: float
    dbcv: float           # HDBSCAN relative validity (density-based); higher = better
    median_size: int


def sweep_clustering(
    embeddings: np.ndarray, *, dims_list: list[int], mcs_list: list[int],
    min_samples: int, umap_neighbors: int = 15, on_progress=lambda s: None,
) -> list[SweepResult]:
    """Grid over (``cluster_dims`` by ``min_cluster_size``).

    Reduces once per dimension (the expensive step) and re-clusters cheaply
    across ``min_cluster_size``, so the grid costs ~one UMAP per dimension.
    Each cell reports topic count, noise %, and DBCV (density-based cluster
    validity) — enough to pick a base granularity without labelling.
    """
    import hdbscan

    results: list[SweepResult] = []
    for dims in dims_list:
        p = ClusterParams(cluster_dims=dims, umap_neighbors=umap_neighbors,
                          min_samples=min_samples)
        on_progress(f"UMAP → {dims}D")
        reduced = umap_reduce(embeddings, p)
        for mcs in mcs_list:
            cl = hdbscan.HDBSCAN(
                min_cluster_size=mcs, min_samples=min_samples, metric="euclidean",
                core_dist_n_jobs=-1, cluster_selection_method="leaf",
                gen_min_span_tree=True,
            )
            labels = cl.fit_predict(reduced)
            n = len({int(x) for x in labels if x >= 0})
            noise = float((labels < 0).mean() * 100)
            try:
                dbcv = float(cl.relative_validity_)
            except Exception:
                dbcv = float("nan")
            sizes = [int((labels == k).sum()) for k in range(n)]
            med = int(np.median(sizes)) if sizes else 0
            results.append(SweepResult(dims, mcs, n, noise, dbcv, med))
            on_progress(f"  dims={dims:2d} mcs={mcs:3d}: {n:3d} topics, "
                        f"{noise:4.0f}% noise, DBCV={dbcv:+.3f}, median size {med}")
    return results


def render_sweep(results: list[SweepResult]) -> str:
    ranked = sorted(results, key=lambda r: (-(r.dbcv if r.dbcv == r.dbcv else -9)))
    lines = [
        "# Clustering sweep",
        "",
        "Ranked by **DBCV** (density-based cluster validity, higher = cleaner "
        "separation). Also weigh topic count (legible?) and noise %.",
        "",
        "| cluster_dims | min_cluster_size | topics | noise % | DBCV | median size |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for r in ranked:
        lines.append(
            f"| {r.cluster_dims} | {r.min_cluster_size} | {r.n_clusters} | "
            f"{r.noise_pct:.0f}% | {r.dbcv:+.3f} | {r.median_size} |"
        )
    return "\n".join(lines) + "\n"


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
    cluster_dims: int = typer.Option(
        10, help="UMAP dim to cluster in (mid-dim; 2 over-merges topics)."
    ),
    tiers: str = typer.Option(
        "15,60", help="Coarse hierarchy tiers merged from leaf centroids "
        "(comma-separated cluster counts, broadest first); empty = flat map."
    ),
    embeddings: str = typer.Option(
        "content", help="'content' (re-embed content-only for topics) or 'stored' "
        "(reuse the district-prefixed retrieval vectors)."
    ),
    labels: bool = typer.Option(
        True, "--labels/--no-labels", help="Label topics with Haiku (needs ANTHROPIC_API_KEY)."
    ),
) -> None:
    """Load embeddings → UMAP + HDBSCAN → Haiku labels → JSON for the map."""
    from herald import schools_db

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    if embeddings not in ("content", "stored"):
        raise typer.BadParameter("--embeddings must be 'content' or 'stored'.")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or None if labels else None
    if labels and not api_key:
        console.print("[yellow]ANTHROPIC_API_KEY not set — topics will be unlabelled[/yellow]")
    voyage_key = os.environ.get("VOYAGE_API_KEY") or None
    if embeddings == "content" and not voyage_key:
        raise typer.BadParameter("VOYAGE_API_KEY is required for --embeddings content.")

    params = ClusterParams(
        min_cluster_size=min_cluster_size, min_samples=min_samples,
        umap_neighbors=umap_neighbors, cluster_dims=cluster_dims,
    )
    with schools_db.connect(db_url) as conn:
        rows = load_chunks(conn.cursor(), sample=sample)
    console.print(f"loaded {len(rows)} chunks · embeddings={embeddings}")
    if not rows:
        raise typer.Exit(1)

    emb = None
    if embeddings == "content":
        emb = content_embeddings(voyage_key, rows, on_progress=lambda s: console.print(s))
    export = run_clustering(rows, params, embeddings=emb, api_key=api_key,
                            hierarchy_targets=_ints(tiers) or None,
                            on_progress=lambda s: console.print(s))
    Path(out).write_text(json.dumps(export, separators=(",", ":")), encoding="utf-8")
    size_kb = Path(out).stat().st_size / 1024
    console.print(
        f"[green]wrote[/green] {out} — {export['n_clusters']} topics, "
        f"{export['n_points']} points ({size_kb:.0f} KB)"
    )


def _ints(s: str) -> list[int]:
    return [int(x) for x in s.replace(" ", "").split(",") if x]


@app.command()
def sweep(
    out: str = typer.Option("cluster-sweep.md", help="Markdown table output path."),
    sample: int = typer.Option(
        8000, help="Sweep on this many random chunks (relative ranking holds; faster/cheaper)."
    ),
    dims: str = typer.Option("5,10,15,20", help="cluster_dims values to try."),
    min_cluster_sizes: str = typer.Option("15,30,60,100", help="HDBSCAN sizes to try."),
    min_samples: int = typer.Option(5, help="HDBSCAN min_samples (held fixed across the grid)."),
    embeddings: str = typer.Option("content", help="'content' or 'stored'."),
) -> None:
    """Grid-search cluster_dims by min_cluster_size; report topic count / noise / DBCV."""
    from herald import schools_db

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    voyage_key = os.environ.get("VOYAGE_API_KEY") or None
    if embeddings == "content" and not voyage_key:
        raise typer.BadParameter("VOYAGE_API_KEY is required for --embeddings content.")

    with schools_db.connect(db_url) as conn:
        rows = load_chunks(conn.cursor(), sample=sample)
    console.print(f"loaded {len(rows)} chunks · embeddings={embeddings}")
    if not rows:
        raise typer.Exit(1)

    emb = (content_embeddings(voyage_key, rows, on_progress=lambda s: console.print(s))
           if embeddings == "content"
           else np.vstack([r.embedding for r in rows]).astype(np.float32))

    results = sweep_clustering(
        emb, dims_list=_ints(dims), mcs_list=_ints(min_cluster_sizes),
        min_samples=min_samples, on_progress=lambda s: console.print(s),
    )
    Path(out).write_text(render_sweep(results), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")


if __name__ == "__main__":
    app()
