"""Recompute cluster geometry from active+content chunks only.

The cluster_runs we have were built before OCR quarantine landed, so
stored centroids, date ranges, and sizes reflect quarantined garbage.
This script recomputes everything that can be derived from membership
WITHOUT re-running HDBSCAN/UMAP — cluster identities (which chunk
belongs to which leaf cluster) stay fixed, but every aggregate over
those members is rebuilt from chunks where status='active' AND
content_type=0:

  active_size      — count of contributing chunks
  active_centroid  — mean embedding (weighted up for tiers 1-3 by t0 active_size)
  burstiness       — CV of weekly chunk counts
  drift_cumulative — sum of consecutive-week centroid cosine distances
  drift_net        — first-week → last-week cosine distance
  drift_weeks      — ISO weeks with ≥1 active+content chunk
  active_date_min  — earliest active+content chunk date
  active_date_max  — latest active+content chunk date

Idempotent — safe to re-run after each ingest / quality re-score / new
cluster_run. Uses the active cluster run via active_cluster_run.

Usage:
    uv run scripts/cluster_recompute.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from herald import settings


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)

    try:
        run_id = _active_run(conn)
        print(f"Active run: {run_id}")

        print("Loading active+content chunks (embedding, date, tier labels)...")
        rows = _load_rows(conn, run_id)
        print(f"  {len(rows):,} chunks")
        if not rows:
            print("Nothing to recompute.")
            return

        print("Computing per-cluster geometry across all tiers...")
        # Map each (tier, label) to its computed metrics. Tiers 1-3 take
        # the same chunk-level view; weighting by size is implicit
        # (averaging over more chunks = bigger contribution).
        metrics = _compute_all_tiers(rows)
        print(f"  {len(metrics):,} (tier, label) pairs")

        print("Writing back to clusters...")
        n = _write_back(conn, run_id, metrics)
        print(f"  Updated {n:,} cluster rows.")

    finally:
        conn.close()


def _active_run(conn: psycopg.Connection) -> UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM active_cluster_run WHERE singleton = true")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No active cluster run")
        rid = row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
    conn.commit()
    return rid


def _load_rows(
    conn: psycopg.Connection,
    run_id: UUID,
) -> list[tuple[np.ndarray, date, int, int, int, int]]:
    """(embedding, date_issued, t0, t1, t2, t3) per active+content chunk."""
    out: list[tuple[np.ndarray, date, int, int, int, int]] = []
    with conn.cursor(name="recompute_load_chunks") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT chunks.embedding,
                   issues.date_issued,
                   chunk_projections.cluster_t0,
                   chunk_projections.cluster_t1,
                   chunk_projections.cluster_t2,
                   chunk_projections.cluster_t3
            FROM chunk_projections
            JOIN chunks ON chunks.id = chunk_projections.chunk_id
            JOIN pages  ON pages.id = chunks.page_id
            JOIN issues ON issues.id = pages.issue_id
            WHERE chunk_projections.run_id = %s
              AND chunks.embedding IS NOT NULL
              AND chunks.status = 'active'
              AND chunk_projections.content_type = 0
            """,
            (run_id,),
        )
        for r in cur:
            emb = np.asarray(r[0], dtype=np.float32)
            out.append((emb, r[1], int(r[2]), int(r[3]), int(r[4]), int(r[5])))
    conn.commit()
    return out


def _iso_week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 1.0
    sim = float(np.dot(a, b) / (na * nb))
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


def _compute_all_tiers(
    rows: list[tuple[np.ndarray, date, int, int, int, int]],
) -> dict[tuple[int, int], dict]:
    """For each (tier, label), compute geometry from member chunks."""

    # Index rows into per-(tier, label) buckets in one pass.
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (_emb, _d, t0, t1, t2, t3) in enumerate(rows):
        for tier, lab in ((0, t0), (1, t1), (2, t2), (3, t3)):
            if lab < 0:
                continue
            buckets[(tier, lab)].append(idx)

    metrics: dict[tuple[int, int], dict] = {}
    for (tier, lab), member_idxs in buckets.items():
        if not member_idxs:
            continue
        embs = np.stack([rows[i][0] for i in member_idxs])
        dates = [rows[i][1] for i in member_idxs]

        centroid = embs.mean(axis=0).astype(np.float32)
        date_min = min(dates)
        date_max = max(dates)

        # Weekly bucketing for burstiness + drift.
        by_week: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, d in zip(member_idxs, dates):
            by_week[_iso_week_key(d)].append(i)
        weeks_sorted = sorted(by_week.keys())
        weekly_counts = [len(by_week[w]) for w in weeks_sorted]
        burstiness = _cv(weekly_counts)

        if len(weeks_sorted) >= 2:
            weekly_centroids = [
                np.stack([rows[i][0] for i in by_week[w]]).mean(axis=0)
                for w in weeks_sorted
            ]
            cum = 0.0
            for i in range(1, len(weekly_centroids)):
                cum += _cosine_distance(weekly_centroids[i - 1], weekly_centroids[i])
            net = _cosine_distance(weekly_centroids[0], weekly_centroids[-1])
        else:
            cum = None
            net = None

        metrics[(tier, lab)] = {
            "active_size": len(member_idxs),
            "active_centroid": centroid,
            "burstiness": burstiness,
            "drift_cumulative": cum,
            "drift_net": net,
            "drift_weeks": len(weeks_sorted),
            "active_date_min": date_min,
            "active_date_max": date_max,
        }
    return metrics


def _cv(counts: list[int]) -> float:
    if not counts:
        return 0.0
    total = sum(counts)
    if total == 0:
        return 0.0
    mean = total / len(counts)
    if mean == 0:
        return 0.0
    var = sum((c - mean) ** 2 for c in counts) / len(counts)
    return (var ** 0.5) / mean


def _write_back(
    conn: psycopg.Connection,
    run_id: UUID,
    metrics: dict[tuple[int, int], dict],
) -> int:
    n = 0
    with conn.transaction():
        cur = conn.cursor()

        # Zero out any cluster that has no active+content members. The
        # brief route uses active_size > 0 as eligibility, so this
        # ensures fully-quarantined clusters drop out cleanly.
        cur.execute(
            """
            UPDATE clusters
               SET active_size = 0,
                   active_centroid = NULL,
                   burstiness = 0,
                   drift_cumulative = NULL,
                   drift_net = NULL,
                   drift_weeks = 0,
                   active_date_min = NULL,
                   active_date_max = NULL
             WHERE run_id = %s
            """,
            (run_id,),
        )

        for (tier, lab), m in metrics.items():
            cur.execute(
                """
                UPDATE clusters
                   SET active_size      = %s,
                       active_centroid  = %s,
                       burstiness       = %s,
                       drift_cumulative = %s,
                       drift_net        = %s,
                       drift_weeks      = %s,
                       active_date_min  = %s,
                       active_date_max  = %s
                 WHERE run_id = %s AND tier = %s AND label = %s
                """,
                (
                    m["active_size"],
                    m["active_centroid"].tolist(),
                    m["burstiness"],
                    m["drift_cumulative"],
                    m["drift_net"],
                    m["drift_weeks"],
                    m["active_date_min"],
                    m["active_date_max"],
                    run_id, tier, lab,
                ),
            )
            n += 1
    return n


if __name__ == "__main__":
    main()
