"""Export Herald's own cluster labels to Markdown for side-by-side
comparison with BERTopic's output.

THROWAWAY ANALYSIS CODE — not integrated into the app.

Pulls the active cluster run's labels from Supabase and writes a
Markdown report sorted by tier, then by size (largest first) within
each tier. Format mirrors scripts/bertopic_diagnostic.py so the two
zips can be opened side by side.

Usage:
    uv run scripts/export_cluster_labels.py

Output: scripts/cluster_labels_YYYYMMDD-HHMMSS.md
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import psycopg

from schoolsengine import settings


SCRIPT_DIR = Path(__file__).parent


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    print("Loading active cluster run + cluster labels from Supabase...")
    run_id, clusters = load_clusters(cfg.supabase_db_url)
    print(f"  Loaded {len(clusters):,} cluster rows across {len({c['tier'] for c in clusters})} tiers")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = SCRIPT_DIR / f"cluster_labels_{timestamp}.md"
    print(f"Writing report → {output_path}")
    write_report(run_id, clusters, output_path)
    print("Done.")


def load_clusters(db_url: str) -> tuple[UUID, list[dict]]:
    conn = psycopg.connect(db_url, autocommit=False, prepare_threshold=None)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id FROM active_cluster_run WHERE singleton = true")
            row = cur.fetchone()
            if not row:
                raise RuntimeError("No active cluster run")
            run_id = row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))

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
    finally:
        conn.close()
    return run_id, clusters


def write_report(run_id: UUID, clusters: list[dict], output_path: Path) -> None:
    lines: list[str] = []
    lines.append("# Herald cluster labels")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Active run: `{run_id}`")
    lines.append("")

    by_tier: dict[int, list[dict]] = {}
    for c in clusters:
        by_tier.setdefault(c["tier"], []).append(c)

    tier_descriptions = {
        0: "Fine — HDBSCAN leaf clusters. Most comparable to BERTopic.",
        1: "Medium — agglomerative merge, weighted centroids.",
        2: "Broad — agglomerative merge, weighted centroids.",
        3: "Macro — agglomerative merge, weighted centroids.",
    }

    total_labeled = sum(1 for c in clusters if c["label_text"])
    total = len(clusters)
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total cluster rows: **{total:,}**")
    lines.append(f"- Rows with Haiku labels: **{total_labeled:,}** ({100 * total_labeled / total:.0f}%)")
    for tier in sorted(by_tier):
        n_t = len(by_tier[tier])
        n_t_labeled = sum(1 for c in by_tier[tier] if c["label_text"])
        lines.append(f"- Tier {tier}: {n_t:,} clusters, {n_t_labeled:,} labeled")
    lines.append("")

    for tier in sorted(by_tier):
        rows = by_tier[tier]
        desc = tier_descriptions.get(tier, "")
        lines.append(f"## Tier {tier} — {len(rows):,} clusters")
        if desc:
            lines.append("")
            lines.append(f"*{desc}*")
        lines.append("")

        for c in rows:
            label_text = c["label_text"] or "_(no label — Haiku skipped or refused)_"
            date_range = ""
            if c["date_min"] and c["date_max"]:
                date_range = (
                    f" &middot; {c['date_min'].isoformat()} → {c['date_max'].isoformat()}"
                )
            lines.append(
                f"- **{label_text}** — n={c['size']:,} (id #{c['label']}){date_range}"
            )
        lines.append("")

    output_path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
