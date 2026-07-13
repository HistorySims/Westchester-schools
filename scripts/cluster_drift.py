"""Per-cluster semantic drift diagnostic.

For every cluster in the active run we:
  1. fetch member chunk embeddings + dates,
  2. bin by ISO week,
  3. compute each week's centroid (mean embedding),
  4. compute the cumulative cosine distance between consecutive weeks,
     and the net cosine distance from first to last week.

We persist both metrics on `clusters.drift_cumulative` and
`clusters.drift_net` (plus `drift_weeks`) and print a Markdown report
that highlights known story-shape comparisons (Voorhees, Anti-Rent,
shipping notices) alongside the top-10 highest- and lowest-drift
clusters per tier.

Idempotent — re-run any time after a cluster run completes.

Usage:
    uv run scripts/cluster_drift.py [--tier N] [--no-write]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from herald import settings


SCRIPT_DIR = Path(__file__).parent

# Labels we want called out at the top of the report, matched
# case-insensitively as substrings on clusters.label_text.
HIGHLIGHT_PATTERNS = [
    "voorhees",
    "anti-rent",
    "antirent",
    "shipping",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tier",
        type=int,
        default=None,
        help="Restrict drift computation to a single tier (0-3).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print metrics without writing to clusters.drift_* columns.",
    )
    args = parser.parse_args()

    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)

    try:
        run_id = _active_run(conn)
        print(f"Active run: {run_id}")

        print("Loading chunk projections + embeddings + dates...")
        rows = _load_chunk_rows(conn, run_id)
        print(f"  Loaded {len(rows):,} chunks")

        print("Loading cluster metadata...")
        clusters = _load_clusters(conn, run_id, args.tier)
        print(f"  Loaded {len(clusters):,} cluster rows")

        print("Computing per-cluster drift...")
        metrics = _compute_drift_per_cluster(rows, clusters)
        n_computed = sum(1 for m in metrics.values() if m["weeks"] >= 2)
        print(f"  Computed drift for {n_computed:,} clusters with ≥2 weeks of data")

        if not args.no_write:
            print("Writing drift_cumulative / drift_net / drift_weeks to clusters...")
            _write_metrics(conn, run_id, metrics)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_path = SCRIPT_DIR / f"cluster_drift_{timestamp}.md"
        _write_report(run_id, clusters, metrics, output_path)
        print(f"Wrote report → {output_path}")

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


def _load_chunk_rows(
    conn: psycopg.Connection,
    run_id: UUID,
) -> list[tuple[np.ndarray, date, int, int, int, int]]:
    """Returns list of (embedding, date_issued, t0, t1, t2, t3).

    Filtered to active (non-quarantined) chunks with content_type=0
    (i.e. drops OCR-garbage clusters and ad/legal-dominated bins). This
    is the correct denominator for "what was this cluster about and how
    did it move", since the corpus already considers the other content
    types non-substantive.
    """
    rows: list[tuple[np.ndarray, date, int, int, int, int]] = []
    with conn.cursor(name="drift_load_chunks") as cur:
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
            rows.append((emb, r[1], int(r[2]), int(r[3]), int(r[4]), int(r[5])))
    conn.commit()
    return rows


def _load_clusters(
    conn: psycopg.Connection,
    run_id: UUID,
    tier_filter: int | None,
) -> list[dict]:
    with conn.cursor() as cur:
        if tier_filter is not None:
            cur.execute(
                """
                SELECT tier, label, size, label_text, date_min, date_max
                FROM clusters
                WHERE run_id = %s AND tier = %s
                ORDER BY tier ASC, size DESC
                """,
                (run_id, tier_filter),
            )
        else:
            cur.execute(
                """
                SELECT tier, label, size, label_text, date_min, date_max
                FROM clusters
                WHERE run_id = %s
                ORDER BY tier ASC, size DESC
                """,
                (run_id,),
            )
        clusters = [
            {
                "tier": int(r[0]),
                "label": int(r[1]),
                "size": int(r[2]),
                "label_text": r[3],
                "date_min": r[4],
                "date_max": r[5],
            }
            for r in cur.fetchall()
        ]
    conn.commit()
    return clusters


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


def _drift_for_member_embeddings(
    embeddings: list[np.ndarray],
    dates: list[date],
) -> dict:
    """Bin by ISO week, return drift metrics.

    Three metrics:
      cum: sum of consecutive-week cosine distances (path length)
      net: cosine distance from first-week to last-week centroid
      ratio = net / cum: in [0, 1] by triangle inequality. High = the
        story is moving in a coherent direction. Low = random walk
        (rotating content with no net displacement, e.g. police-blotter
        cluster where each week is a different incident).
    """
    by_week: dict[tuple[int, int], list[np.ndarray]] = defaultdict(list)
    for emb, d in zip(embeddings, dates):
        by_week[_iso_week_key(d)].append(emb)

    if len(by_week) < 2:
        return {
            "weeks": len(by_week),
            "cumulative": None,
            "net": None,
            "ratio": None,
            "n_chunks": len(embeddings),
        }

    sorted_weeks = sorted(by_week.keys())
    centroids = [np.mean(np.stack(by_week[w]), axis=0) for w in sorted_weeks]

    cumulative = 0.0
    for i in range(1, len(centroids)):
        cumulative += _cosine_distance(centroids[i - 1], centroids[i])
    net = _cosine_distance(centroids[0], centroids[-1])
    ratio = _safe_ratio(net, cumulative)

    return {
        "weeks": len(sorted_weeks),
        "cumulative": cumulative,
        "net": net,
        "ratio": ratio,
        "n_chunks": len(embeddings),
    }


def _safe_ratio(net: float, cum: float) -> float | None:
    """net / cum, capped at 1.0 for numerical safety, None when cum≈0."""
    if cum is None or cum <= 1e-9:
        return None
    return min(1.0, net / cum)


def _compute_drift_per_cluster(
    rows: list[tuple[np.ndarray, date, int, int, int, int]],
    clusters: list[dict],
) -> dict[tuple[int, int], dict]:
    """Returns {(tier, label): metrics_dict}."""
    members: dict[tuple[int, int], tuple[list[np.ndarray], list[date]]] = defaultdict(
        lambda: ([], [])
    )
    wanted = {(c["tier"], c["label"]) for c in clusters}

    for emb, d, t0, t1, t2, t3 in rows:
        for tier, lab in ((0, t0), (1, t1), (2, t2), (3, t3)):
            if lab < 0:
                continue
            if (tier, lab) not in wanted:
                continue
            embs, dates = members[(tier, lab)]
            embs.append(emb)
            dates.append(d)

    metrics: dict[tuple[int, int], dict] = {}
    for key, (embs, dates) in members.items():
        metrics[key] = _drift_for_member_embeddings(embs, dates)
    for c in clusters:
        key = (c["tier"], c["label"])
        if key not in metrics:
            metrics[key] = {"weeks": 0, "cumulative": None, "net": None, "n_chunks": 0}
    return metrics


def _write_metrics(
    conn: psycopg.Connection,
    run_id: UUID,
    metrics: dict[tuple[int, int], dict],
) -> None:
    with conn.transaction():
        cur = conn.cursor()
        for (tier, lab), m in metrics.items():
            cur.execute(
                """
                UPDATE clusters
                   SET drift_cumulative = %s,
                       drift_net        = %s,
                       drift_weeks      = %s
                 WHERE run_id = %s AND tier = %s AND label = %s
                """,
                (
                    m["cumulative"],
                    m["net"],
                    m["weeks"],
                    run_id, tier, lab,
                ),
            )


def _matches_highlight(label_text: str | None) -> str | None:
    if not label_text:
        return None
    low = label_text.lower()
    for pat in HIGHLIGHT_PATTERNS:
        if pat in low:
            return pat
    return None


def _write_report(
    run_id: UUID,
    clusters: list[dict],
    metrics: dict[tuple[int, int], dict],
    output_path: Path,
) -> None:
    lines: list[str] = []
    lines.append("# Herald cluster semantic drift")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Active run: `{run_id}`")
    lines.append("")
    lines.append(
        "Members are filtered to `chunks.status='active'` AND "
        "`content_type=0` (drops quarantined OCR-garbage and ad/legal "
        "chunks). For each cluster we bin its members by ISO week, "
        "compute the centroid per week, then report:"
    )
    lines.append("")
    lines.append("- **cum**: sum of cosine distances between consecutive weekly centroids (path length)")
    lines.append("- **net**: cosine distance from first-week centroid to last-week centroid (displacement)")
    lines.append("- **ratio**: net / cum, in [0, 1]. High = directional evolution; low = random walk (rotating content with no net displacement)")
    lines.append("- **n**: active+content chunks contributing to the centroids")
    lines.append("- **weeks**: number of ISO weeks with ≥1 such chunk")
    lines.append("")

    # --- Highlights ---
    highlights = [c for c in clusters if _matches_highlight(c["label_text"])]
    highlights.sort(key=lambda c: (c["tier"], -c["size"]))
    lines.append("## Highlight clusters (known story-shapes)")
    lines.append("")
    if not highlights:
        lines.append("_No clusters matched the highlight patterns._")
        lines.append("")
    else:
        lines.append("| tier | n | label | weeks | cum | net | ratio | matched |")
        lines.append("| ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |")
        for c in highlights:
            m = metrics.get((c["tier"], c["label"]), {})
            lines.append(_format_row(c, m))
        lines.append("")

    # --- Per-tier rankings: net displacement vs net/cum ratio ---
    by_tier: dict[int, list[dict]] = defaultdict(list)
    for c in clusters:
        by_tier[c["tier"]].append(c)

    for tier in sorted(by_tier):
        rows = by_tier[tier]
        scored: list[tuple[dict, dict]] = [
            (c, metrics.get((c["tier"], c["label"]), {})) for c in rows
        ]
        # Eligibility: ≥30 active+content chunks contributed to drift (not stored size).
        scored = [
            (c, m) for c, m in scored
            if m.get("cumulative") is not None
            and m.get("n_chunks", 0) >= 30
        ]
        if not scored:
            continue
        lines.append(f"## Tier {tier} — directional evolution (active+content, n ≥ 30)")
        lines.append("")
        lines.append(
            "_Read side by side: clusters that top BOTH lists are genuinely "
            "evolving (Voorhees/Mexico shape). High net but low ratio is "
            "churn (police-blotter/markets — rotating content, no coherent "
            "direction)._"
        )
        lines.append("")

        by_net = sorted(scored, key=lambda cm: -(cm[1]["net"] or 0))[:15]
        by_ratio = sorted(scored, key=lambda cm: -(cm[1].get("ratio") or 0))[:15]

        lines.append("### Top 15 by net displacement")
        lines.append("")
        lines.append("| n | label | weeks | cum | net | ratio |")
        lines.append("| ---: | --- | ---: | ---: | ---: | ---: |")
        for c, m in by_net:
            lines.append(_format_row(c, m, include_tier=False, include_match=False))
        lines.append("")

        lines.append("### Top 15 by net/cum ratio (directionality)")
        lines.append("")
        lines.append("| n | label | weeks | cum | net | ratio |")
        lines.append("| ---: | --- | ---: | ---: | ---: | ---: |")
        for c, m in by_ratio:
            lines.append(_format_row(c, m, include_tier=False, include_match=False))
        lines.append("")

    output_path.write_text("\n".join(lines))


def _format_row(
    c: dict,
    m: dict,
    include_tier: bool = True,
    include_match: bool = True,
) -> str:
    label = c["label_text"] or f"_(no label, id #{c['label']})_"
    weeks = m.get("weeks", 0)
    n = m.get("n_chunks", 0)
    cum = m.get("cumulative")
    net = m.get("net")
    ratio = m.get("ratio")
    cum_s = f"{cum:.3f}" if cum is not None else "—"
    net_s = f"{net:.3f}" if net is not None else "—"
    ratio_s = f"{ratio:.2f}" if ratio is not None else "—"

    cells = []
    if include_tier:
        cells.append(str(c["tier"]))
    cells.extend([f"{n:,}", label, str(weeks), cum_s, net_s, ratio_s])
    if include_match:
        cells.append(_matches_highlight(c["label_text"]) or "")
    return "| " + " | ".join(cells) + " |"


if __name__ == "__main__":
    main()
