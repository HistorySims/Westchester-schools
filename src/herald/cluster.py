"""Hierarchical clustering + UMAP projection batch pipeline.

Reads all chunk embeddings from the database, computes:
1. HDBSCAN base clusters (tier 0) with leaf method + preserved -1 outlier bin
2. Agglomerative merge hierarchy (tiers 1-3) with size-weighted centroids
3. UMAP 2D projection for visualization
4. Content-type classification (ads, legal, bad OCR)

Outlier chunks (HDBSCAN noise, label -1) keep label -1 at all tiers
so they remain visually distinguishable in the UI.

Results are written to cluster_runs, clusters, and chunk_projections tables.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from herald.classify import classify_chunk


@dataclass
class ClusterParams:
    min_cluster_size: int = 15
    min_samples: int = 5
    umap_neighbors: int = 15
    umap_min_dist: float = 0.1
    tier1_target: int = 50
    tier2_target: int = 15
    tier3_target: int = 5


@dataclass
class ClusterResult:
    run_id: UUID
    chunk_count: int
    tier_counts: dict[int, int] = field(default_factory=dict)
    content_type_counts: dict[int, int] = field(default_factory=dict)
    outlier_count: int = 0
    labels_generated: int = 0


LABEL_SYSTEM_PROMPT = """You will be given several short passages from 1840s New York newspapers grouped by semantic similarity into a single cluster. The passages are weighted samples — the most numerous sub-topics in this cluster contribute more passages, so the dominant theme should be obvious.

Your job: identify the cluster's shared theme in 3 to 10 words.

If the passages tightly converge on one event or topic (typical of small, fine-grained clusters): be specific. Name people, places, dates, events.
- "Sheriff Steele killing in Andes, August 1845"
- "Smith Boughton arrest and trial"
- "Texas annexation Senate debate"
- "Shipping arrivals at port of New York"

If passages span several related sub-topics (typical of broader clusters that merged many fine clusters): name the umbrella theme that connects them.
- "Anti-Rent movement and Hudson Valley tenant unrest"
- "U.S.-Mexican border tensions and Texas annexation"
- "Politics, elections, and party conventions"
- "Crime, courts, and police-court arrests"

Forbidden:
- Generic catch-all phrases: "various 1840s topics", "newspaper articles about politics"
- Meta language: "passages discussing", "articles about", "coverage of"
- Just say the topic.

If the passages are genuinely incoherent — garbled OCR, no recoverable theme — reply with exactly: SKIP

Respond with ONLY the label or SKIP."""


LABEL_MIN_SIZE = 20
LABEL_REP_CHUNKS_BY_TIER = {0: 5, 1: 8, 2: 12, 3: 15}
LABEL_MAX_CONCURRENT = 1
LABEL_MIN_INTERVAL_SECS = 1.3
LABEL_MAX_RETRIES = 2

import re as _re

_LABEL_REFUSAL_PATTERNS = [
    _re.compile(r"^i cannot\b", _re.IGNORECASE),
    _re.compile(r"^i'?m unable\b", _re.IGNORECASE),
    _re.compile(r"^unable to\b", _re.IGNORECASE),
    _re.compile(r"^the passages\b.*\b(do not|don't)\b", _re.IGNORECASE),
    _re.compile(r"\bocr[- ]?(damaged|corrupted|errors?)\b", _re.IGNORECASE),
    _re.compile(r"\bseverely corrupted\b", _re.IGNORECASE),
    _re.compile(r"\bcannot reliably\b", _re.IGNORECASE),
    _re.compile(r"\bunintelligible\b", _re.IGNORECASE),
    _re.compile(r"\bno (clear|shared|coherent)\b", _re.IGNORECASE),
    _re.compile(r"^unclear\b", _re.IGNORECASE),
]


def _is_refusal(label: str) -> bool:
    return any(p.search(label) for p in _LABEL_REFUSAL_PATTERNS)


def run_labels_only(
    db_url: str,
    on_progress: Callable[[str], None] | None = None,
) -> int:
    """Regenerate labels for the active cluster run without re-clustering.

    Loads embeddings + chunk_projections from the active run,
    re-derives cluster centroids and representative chunks, and
    writes labels to clusters.label_text.

    Returns the number of labels written.
    """
    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    conn = psycopg.connect(db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM active_cluster_run WHERE singleton = true")
            row = cur.fetchone()
            if not row:
                raise ValueError("No active cluster run")
            run_id = row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
        conn.commit()
        log(f"Using active run {run_id}")

        log("Loading chunks + projections (active + content_type=0 only)...")
        # Filter to status='active' AND content_type=0 so the rep-chunk
        # sampler doesn't draw from quarantined OCR garbage. Clusters
        # that have no active members after this filter are skipped
        # entirely — _build_label_items already enforces a min-size
        # threshold and zero-active clusters fall below it.
        chunk_data: list[tuple[UUID, list[float], str, int, int, int, int]] = []
        with conn.cursor(name="load_for_labels") as cur:
            cur.itersize = 5000
            cur.execute(
                """
                SELECT cp.chunk_id, chunks.embedding, chunks.content,
                       cp.cluster_t0, cp.cluster_t1, cp.cluster_t2, cp.cluster_t3
                FROM chunk_projections cp
                JOIN chunks ON chunks.id = cp.chunk_id
                WHERE cp.run_id = %s
                  AND chunks.status = 'active'
                  AND cp.content_type = 0
                ORDER BY cp.chunk_id
                """,
                (run_id,),
            )
            for r in cur:
                chunk_data.append(r)
        conn.commit()
        log(f"Loaded {len(chunk_data)} chunks")

        if not chunk_data:
            return 0

        embeddings = np.array([r[1] for r in chunk_data], dtype=np.float32)
        contents = [r[2] for r in chunk_data]
        tier_labels = np.array(
            [[r[3], r[4], r[5], r[6]] for r in chunk_data], dtype=np.int32
        )

        # Reconstruct t0 centroids, sizes, and hierarchy so we can reuse
        # the same weighted-sampling logic as run_pipeline.
        t0_array = tier_labels[:, 0]
        n_t0 = int(t0_array.max()) + 1 if t0_array.max() >= 0 else 0
        if n_t0 == 0:
            log("No tier-0 clusters in active run; nothing to label")
            return 0

        t0_centroids = np.zeros((n_t0, embeddings.shape[1]), dtype=np.float32)
        t0_sizes = np.zeros(n_t0, dtype=np.int64)
        for lab in range(n_t0):
            mask = t0_array == lab
            count = int(mask.sum())
            if count == 0:
                continue
            t0_centroids[lab] = embeddings[mask].mean(axis=0)
            t0_sizes[lab] = count

        hierarchy: dict[int, dict[int, int]] = {1: {}, 2: {}, 3: {}}
        for chunk_row in range(len(chunk_data)):
            t0 = int(tier_labels[chunk_row, 0])
            if t0 < 0:
                continue
            for tier in (1, 2, 3):
                upper = int(tier_labels[chunk_row, tier])
                if upper >= 0:
                    hierarchy[tier][t0] = upper

        items = _build_label_items(
            t0_array, contents, embeddings,
            t0_centroids, t0_sizes, hierarchy,
        )

        log(f"Labeling {len(items)} clusters (concurrent={LABEL_MAX_CONCURRENT})...")
        results = asyncio.run(_label_clusters_async(api_key, items, log))

        written = 0
        with conn.transaction():
            cur = conn.cursor()
            for tier_idx, cluster_label, text in results:
                if text is None:
                    continue
                cur.execute(
                    "UPDATE clusters SET label_text = %s WHERE run_id = %s AND tier = %s AND label = %s",
                    (text, run_id, tier_idx, cluster_label),
                )
                written += 1
        log(f"Wrote {written} labels")
        return written

    finally:
        conn.close()


def run_pipeline(
    db_url: str,
    params: ClusterParams | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ClusterResult:
    if params is None:
        params = ClusterParams()

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    conn = psycopg.connect(db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)

    try:
        log("Loading chunk data from database...")
        chunk_ids, embeddings, dates, contents = _load_chunks(conn, log)
        n = len(chunk_ids)
        log(f"Loaded {n} chunks with embeddings")

        if n == 0:
            raise ValueError("No chunks with embeddings found in database")

        log("Classifying content types...")
        content_types = np.array(
            [classify_chunk(c) for c in contents], dtype=np.int8
        )
        type_counts = {}
        for t in range(4):
            count = int(np.sum(content_types == t))
            type_counts[t] = count
        log(f"  content={type_counts.get(0,0)} ad={type_counts.get(1,0)} "
            f"legal={type_counts.get(2,0)} bad_ocr={type_counts.get(3,0)}")

        log(f"Running HDBSCAN (leaf, min_cluster_size={params.min_cluster_size})...")
        t0_labels = _hdbscan_cluster(embeddings, params)
        outlier_count = int(np.sum(t0_labels == -1))
        n_real_clusters = int(t0_labels.max()) + 1 if t0_labels.max() >= 0 else 0
        log(f"  {n_real_clusters} clusters + {outlier_count} outliers ({100*outlier_count/n:.1f}%)")

        log("Computing tier-0 weighted centroids (outliers excluded)...")
        t0_centroids, t0_sizes, t0_date_ranges = _compute_cluster_stats(
            embeddings, t0_labels, dates, n_real_clusters
        )

        log("Building agglomerative hierarchy (tiers 1-3)...")
        hierarchy = _build_hierarchy(
            t0_centroids, t0_sizes, n_real_clusters, params
        )

        log(f"Running UMAP (n_neighbors={params.umap_neighbors})...")
        xy = _umap_project(embeddings, params)
        log(f"  UMAP complete, shape={xy.shape}")

        log("Writing results to database...")
        run_id = _write_results(
            conn, params, chunk_ids, xy, t0_labels,
            hierarchy, t0_centroids, t0_sizes, t0_date_ranges,
            content_types, n_real_clusters, log,
        )

        result = ClusterResult(
            run_id=run_id,
            chunk_count=n,
            tier_counts={0: n_real_clusters},
            content_type_counts=type_counts,
            outlier_count=outlier_count,
        )
        for tier, mapping in hierarchy.items():
            result.tier_counts[tier] = len(set(mapping.values()))

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            log("Building label items...")
            items = _build_label_items(
                t0_labels, contents, embeddings,
                t0_centroids, t0_sizes, hierarchy,
            )
            log(f"Labeling {len(items)} clusters via Haiku (concurrent={LABEL_MAX_CONCURRENT})...")
            results = asyncio.run(_label_clusters_async(anthropic_key, items, log))
            written = 0
            with conn.transaction():
                cur = conn.cursor()
                for tier, label, text in results:
                    if text is None:
                        continue
                    cur.execute(
                        "UPDATE clusters SET label_text = %s WHERE run_id = %s AND tier = %s AND label = %s",
                        (text, run_id, tier, label),
                    )
                    written += 1
            result.labels_generated = written
            log(f"  Wrote {written} cluster labels")
        else:
            log("ANTHROPIC_API_KEY not set, skipping cluster labels")

        log(f"Done. run_id={run_id}")
        return result

    finally:
        conn.close()


def _load_chunks(
    conn: psycopg.Connection,
    log: Callable[[str], None],
) -> tuple[list[UUID], np.ndarray, list[date], list[str]]:
    chunk_ids: list[UUID] = []
    embeddings_list: list[list[float]] = []
    dates_list: list[date] = []
    contents_list: list[str] = []

    with conn.cursor(name="load_chunks") as cur:
        cur.itersize = 5000
        cur.execute("""
            SELECT c.id, c.embedding, i.date_issued, c.content
            FROM chunks c
            JOIN pages p ON p.id = c.page_id
            JOIN issues i ON i.id = p.issue_id
            WHERE c.is_current = true
              AND c.embedding IS NOT NULL
            ORDER BY c.id
        """)
        batch = 0
        for row in cur:
            chunk_ids.append(row[0] if isinstance(row[0], UUID) else UUID(str(row[0])))
            embeddings_list.append(row[1])
            dates_list.append(row[2])
            contents_list.append(row[3])
            batch += 1
            if batch % 10000 == 0:
                log(f"  loaded {batch} chunks...")

    conn.commit()
    embeddings = np.array(embeddings_list, dtype=np.float32)
    return chunk_ids, embeddings, dates_list, contents_list


async def _label_clusters_async(
    api_key: str,
    items: list[dict],
    log: Callable[[str], None],
) -> list[tuple[int, int, str | None]]:
    """Generate labels for clusters using Haiku. Returns [(tier, label, label_text), ...]."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0)
    semaphore = asyncio.Semaphore(LABEL_MAX_CONCURRENT)
    done_count = [0]
    error_counts: dict[str, int] = defaultdict(int)
    last_call_at = [0.0]  # mutable wall-clock of last issued call (monotonic seconds)
    pace_lock = asyncio.Lock()

    async def _pace() -> None:
        async with pace_lock:
            import time as _time
            now = _time.monotonic()
            gap = now - last_call_at[0]
            if gap < LABEL_MIN_INTERVAL_SECS:
                await asyncio.sleep(LABEL_MIN_INTERVAL_SECS - gap)
            last_call_at[0] = _time.monotonic()

    async def label_one(item):
        async with semaphore:
            await _pace()
            user_msg = "\n\n---\n\n".join(item["contents"])
            label = None
            for attempt in range(LABEL_MAX_RETRIES + 1):
                try:
                    msg = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=40,
                        temperature=0,
                        system=LABEL_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_msg}],
                    )
                    text = ""
                    for block in msg.content:
                        if hasattr(block, "text"):
                            text += block.text
                    if text.strip():
                        label = text.strip().splitlines()[0].strip()
                        if label and label.upper().strip(".") == "SKIP":
                            error_counts["skipped"] += 1
                            label = None
                        elif label and _is_refusal(label):
                            error_counts["refused"] += 1
                            label = None
                        if label and len(label) > 120:
                            label = label[:120]
                    break
                except anthropic.RateLimitError:
                    if attempt < LABEL_MAX_RETRIES:
                        # 60s minimum on rate-limit retry: Anthropic's RPM window
                        # is per-minute, so anything shorter just hits again.
                        await asyncio.sleep(60 * (attempt + 1))
                        continue
                    error_counts["rate_limit"] += 1
                    break
                except anthropic.APIError as e:
                    error_counts[f"api_{type(e).__name__}"] += 1
                    break
                except Exception as e:
                    error_counts[type(e).__name__] += 1
                    break
            done_count[0] += 1
            if done_count[0] % 20 == 0:
                log(f"    labeled {done_count[0]}/{len(items)}")
            return (item["tier"], item["label"], label)

    results = await asyncio.gather(*[label_one(it) for it in items])
    success = sum(1 for _, _, lbl in results if lbl is not None)
    log(f"    label results: {success} succeeded, {len(results) - success} failed")
    if error_counts:
        log(f"    errors: {dict(error_counts)}")
    return results


def _pick_top_closest(
    member_indices: list[int],
    embeddings: np.ndarray,
    centroid: np.ndarray,
    n: int,
) -> list[int]:
    if n <= 0 or not member_indices:
        return []
    member_arr = np.array(member_indices)
    member_emb = embeddings[member_arr]
    dists = np.linalg.norm(member_emb - centroid, axis=1)
    top = np.argsort(dists)[:n]
    return [int(member_arr[i]) for i in top]


def _build_label_items(
    t0_labels: np.ndarray,
    contents: list[str],
    embeddings: np.ndarray,
    t0_centroids: np.ndarray,
    t0_sizes: np.ndarray,
    hierarchy: dict[int, dict[int, int]],
) -> list[dict]:
    """For each cluster, pick representative chunks (weighted by sub-cluster size at higher tiers)."""
    items: list[dict] = []

    t0_members: dict[int, list[int]] = defaultdict(list)
    for i in range(len(t0_labels)):
        lab = int(t0_labels[i])
        if lab >= 0:
            t0_members[lab].append(i)

    # Tier 0: closest-to-centroid (each cluster is one tight topic)
    rep_count_0 = LABEL_REP_CHUNKS_BY_TIER[0]
    for t0_lab, members in t0_members.items():
        if t0_sizes[t0_lab] < LABEL_MIN_SIZE:
            continue
        picked = _pick_top_closest(members, embeddings, t0_centroids[t0_lab], rep_count_0)
        rep_contents = [contents[i][:400] for i in picked]
        items.append({"tier": 0, "label": t0_lab, "contents": rep_contents})

    # Higher tiers: pick chunks PROPORTIONALLY from sub-clusters by their size.
    # This preserves the NYC/Syracuse/Buffalo weighting: a sub-cluster with 1000
    # chunks contributes far more rep chunks than one with 30, so the dominant
    # theme drowns out fringe sub-topics in the prompt.
    for tier in [1, 2, 3]:
        mapping = hierarchy.get(tier, {})
        sub_by_upper: dict[int, list[int]] = defaultdict(list)
        for t0_lab, upper_lab in mapping.items():
            sub_by_upper[upper_lab].append(t0_lab)

        rep_total = LABEL_REP_CHUNKS_BY_TIER[tier]

        for upper_lab, sub_t0_labs in sub_by_upper.items():
            total_size = sum(int(t0_sizes[t0]) for t0 in sub_t0_labs)
            if total_size < LABEL_MIN_SIZE:
                continue

            sub_t0_labs.sort(key=lambda t0: -int(t0_sizes[t0]))

            picked: list[int] = []
            remaining = rep_total
            for idx, t0_lab in enumerate(sub_t0_labs):
                if remaining <= 0:
                    break
                size = int(t0_sizes[t0_lab])
                share = max(
                    1 if idx < 3 else 0,
                    round(rep_total * size / total_size),
                )
                share = min(share, remaining)
                if share <= 0:
                    continue
                picked_here = _pick_top_closest(
                    t0_members.get(t0_lab, []),
                    embeddings,
                    t0_centroids[t0_lab],
                    share,
                )
                picked.extend(picked_here)
                remaining -= len(picked_here)

            if not picked:
                continue
            rep_contents = [contents[i][:400] for i in picked]
            items.append({"tier": tier, "label": upper_lab, "contents": rep_contents})

    return items


def _hdbscan_cluster(
    embeddings: np.ndarray,
    params: ClusterParams,
) -> np.ndarray:
    import hdbscan

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=params.min_cluster_size,
        min_samples=params.min_samples,
        metric="euclidean",
        core_dist_n_jobs=-1,
        cluster_selection_method="leaf",
    )
    return clusterer.fit_predict(embeddings)


def _compute_cluster_stats(
    embeddings: np.ndarray,
    labels: np.ndarray,
    dates: list[date],
    n_clusters: int,
) -> tuple[np.ndarray, np.ndarray, list[tuple[date, date]]]:
    if n_clusters == 0:
        return (
            np.zeros((0, embeddings.shape[1]), dtype=np.float32),
            np.zeros(0, dtype=np.int64),
            [],
        )

    dim = embeddings.shape[1]
    centroids = np.zeros((n_clusters, dim), dtype=np.float64)
    sizes = np.zeros(n_clusters, dtype=np.int64)
    date_ranges: list[tuple[date, date]] = []

    date_mins: dict[int, date] = {}
    date_maxs: dict[int, date] = {}

    for i in range(len(labels)):
        lab = int(labels[i])
        if lab < 0:
            continue
        centroids[lab] += embeddings[i]
        sizes[lab] += 1
        d = dates[i]
        if lab not in date_mins or d < date_mins[lab]:
            date_mins[lab] = d
        if lab not in date_maxs or d > date_maxs[lab]:
            date_maxs[lab] = d

    for lab in range(n_clusters):
        if sizes[lab] > 0:
            centroids[lab] /= sizes[lab]

    for lab in range(n_clusters):
        if lab in date_mins:
            date_ranges.append((date_mins[lab], date_maxs[lab]))
        else:
            date_ranges.append((date(1845, 6, 1), date(1845, 6, 1)))

    return centroids.astype(np.float32), sizes, date_ranges


def _build_hierarchy(
    t0_centroids: np.ndarray,
    t0_sizes: np.ndarray,
    n_t0: int,
    params: ClusterParams,
) -> dict[int, dict[int, int]]:
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    if n_t0 <= 1:
        return {1: {0: 0} if n_t0 else {}, 2: {0: 0} if n_t0 else {}, 3: {0: 0} if n_t0 else {}}

    dists = pdist(t0_centroids, metric="cosine")
    dists = np.nan_to_num(dists, nan=1.0)
    Z = linkage(dists, method="average")

    hierarchy: dict[int, dict[int, int]] = {}

    for tier, target in [(1, params.tier1_target), (2, params.tier2_target), (3, params.tier3_target)]:
        target = min(target, n_t0)
        target = max(target, 1)
        tier_labels = fcluster(Z, t=target, criterion="maxclust")
        tier_labels -= 1
        mapping = {t0_lab: int(tier_labels[t0_lab]) for t0_lab in range(n_t0)}
        hierarchy[tier] = mapping

    return hierarchy


def _umap_project(embeddings: np.ndarray, params: ClusterParams) -> np.ndarray:
    import umap

    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=params.umap_neighbors,
        min_dist=params.umap_min_dist,
        metric="cosine",
        random_state=42,
        low_memory=True,
    )
    xy = reducer.fit_transform(embeddings)

    for dim in range(2):
        col = xy[:, dim]
        mn, mx = col.min(), col.max()
        if mx - mn > 1e-10:
            xy[:, dim] = (col - mn) / (mx - mn)
        else:
            xy[:, dim] = 0.5

    return xy.astype(np.float32)


def _write_results(
    conn: psycopg.Connection,
    params: ClusterParams,
    chunk_ids: list[UUID],
    xy: np.ndarray,
    t0_labels: np.ndarray,
    hierarchy: dict[int, dict[int, int]],
    t0_centroids: np.ndarray,
    t0_sizes: np.ndarray,
    t0_date_ranges: list[tuple[date, date]],
    content_types: np.ndarray,
    n_real_clusters: int,
    log: Callable[[str], None],
) -> UUID:
    n = len(chunk_ids)

    with conn.transaction():
        cur = conn.cursor()

        cur.execute(
            """INSERT INTO cluster_runs (chunk_count, params, status)
               VALUES (%s, %s, 'running') RETURNING id""",
            (n, json.dumps({
                "min_cluster_size": params.min_cluster_size,
                "min_samples": params.min_samples,
                "umap_neighbors": params.umap_neighbors,
                "umap_min_dist": params.umap_min_dist,
                "method": "hdbscan-leaf",
            })),
        )
        run_id = cur.fetchone()[0]
        if not isinstance(run_id, UUID):
            run_id = UUID(str(run_id))

        log(f"  Writing tier-0 clusters ({n_real_clusters})...")
        t0_db_ids: dict[int, UUID] = {}
        for lab in range(n_real_clusters):
            cur.execute(
                """INSERT INTO clusters (run_id, tier, label, size, centroid, date_min, date_max)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    run_id, 0, lab, int(t0_sizes[lab]),
                    t0_centroids[lab].tolist(),
                    t0_date_ranges[lab][0], t0_date_ranges[lab][1],
                ),
            )
            t0_db_ids[lab] = cur.fetchone()[0]
            if not isinstance(t0_db_ids[lab], UUID):
                t0_db_ids[lab] = UUID(str(t0_db_ids[lab]))

        for tier in [1, 2, 3]:
            mapping = hierarchy.get(tier, {})
            tier_labels_set = sorted(set(mapping.values()))
            log(f"  Writing tier-{tier} clusters ({len(tier_labels_set)})...")

            tier_centroids: dict[int, np.ndarray] = {}
            tier_sizes: dict[int, int] = defaultdict(int)
            tier_date_mins: dict[int, date] = {}
            tier_date_maxs: dict[int, date] = {}

            for t0_lab, upper_lab in mapping.items():
                w = int(t0_sizes[t0_lab])
                tier_sizes[upper_lab] += w
                if upper_lab not in tier_centroids:
                    tier_centroids[upper_lab] = t0_centroids[t0_lab].astype(np.float64) * w
                else:
                    tier_centroids[upper_lab] += t0_centroids[t0_lab].astype(np.float64) * w

                dmin, dmax = t0_date_ranges[t0_lab]
                if upper_lab not in tier_date_mins or dmin < tier_date_mins[upper_lab]:
                    tier_date_mins[upper_lab] = dmin
                if upper_lab not in tier_date_maxs or dmax > tier_date_maxs[upper_lab]:
                    tier_date_maxs[upper_lab] = dmax

            for lab in tier_labels_set:
                if tier_sizes[lab] > 0:
                    tier_centroids[lab] /= tier_sizes[lab]

            tier_db_ids: dict[int, UUID] = {}
            for lab in tier_labels_set:
                cur.execute(
                    """INSERT INTO clusters (run_id, tier, label, size, centroid, date_min, date_max)
                       VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (
                        run_id, tier, lab, tier_sizes[lab],
                        tier_centroids[lab].astype(np.float32).tolist(),
                        tier_date_mins.get(lab), tier_date_maxs.get(lab),
                    ),
                )
                tier_db_ids[lab] = cur.fetchone()[0]
                if not isinstance(tier_db_ids[lab], UUID):
                    tier_db_ids[lab] = UUID(str(tier_db_ids[lab]))

            if tier == 1:
                for t0_lab, upper_lab in mapping.items():
                    cur.execute(
                        "UPDATE clusters SET parent_id = %s WHERE id = %s",
                        (tier_db_ids[upper_lab], t0_db_ids[t0_lab]),
                    )
            else:
                prev_mapping = hierarchy[tier - 1]
                prev_to_current: dict[int, int] = {}
                for t0_lab in range(n_real_clusters):
                    prev_lab = prev_mapping[t0_lab]
                    curr_lab = mapping[t0_lab]
                    prev_to_current[prev_lab] = curr_lab

                for prev_lab, curr_lab in prev_to_current.items():
                    cur.execute(
                        "UPDATE clusters SET parent_id = %s WHERE run_id = %s AND tier = %s AND label = %s",
                        (tier_db_ids[curr_lab], run_id, tier - 1, prev_lab),
                    )

        log(f"  Writing {n} chunk projections...")
        t1_map = hierarchy.get(1, {})
        t2_map = hierarchy.get(2, {})
        t3_map = hierarchy.get(3, {})

        batch_size = 5000
        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            batch_params = []
            for i in range(start, end):
                t0 = int(t0_labels[i])
                if t0 < 0:
                    t1 = t2 = t3 = -1
                else:
                    t1 = t1_map.get(t0, -1)
                    t2 = t2_map.get(t0, -1)
                    t3 = t3_map.get(t0, -1)
                batch_params.append((
                    chunk_ids[i], run_id,
                    float(xy[i, 0]), float(xy[i, 1]),
                    t0, t1, t2, t3,
                    int(content_types[i]),
                ))
            cur.executemany(
                """INSERT INTO chunk_projections
                   (chunk_id, run_id, x, y, cluster_t0, cluster_t1, cluster_t2, cluster_t3, content_type)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                batch_params,
            )
            if (end - start) == batch_size:
                log(f"    {end}/{n} projections written...")

        cur.execute(
            "UPDATE cluster_runs SET status = 'completed', finished_at = now() WHERE id = %s",
            (run_id,),
        )

        cur.execute(
            """INSERT INTO active_cluster_run (singleton, run_id)
               VALUES (true, %s)
               ON CONFLICT (singleton) DO UPDATE SET run_id = %s, activated_at = now()""",
            (run_id, run_id),
        )

    return run_id
