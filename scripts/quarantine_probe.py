"""Diagnostic — quarantine bar tuning probe.

Read-only. Reports:
  - Counts by (status, quarantine_reason).
  - Quality-score histogram across all current chunks.
  - For each tier-0 cluster whose label_text is a Haiku refusal:
    chunk count, mean/median dict_word_ratio, mean quality_score.
    These are the clusters the user pointed at — chunks here should
    be quarantined but currently aren't.
  - Projected quarantine counts under candidate new thresholds, so
    we tune from data rather than guess.

No DB writes. Run via the recovery-score or a one-shot workflow.

Usage:
    uv run scripts/quarantine_probe.py
"""

from __future__ import annotations

import json
import sys
from statistics import mean, median

import psycopg

from herald import settings


SCRIPT_DIR_HINT = "scripts/quarantine_probe_report.md"

REFUSAL_NEEDLES = (
    "cannot reliably",
    "i cannot",
    "i'm unable",
    "unable to",
    "ocr-damaged",
    "ocr damaged",
    "ocr-corrupted",
    "severely corrupted",
    "unintelligible",
    "no clear",
    "no shared",
    "no coherent",
)

# Thresholds to evaluate. Each row is (quarantine_dict_ratio,
# require_structural_break). The script tells us how many chunks
# would flip status under each.
CANDIDATE_THRESHOLDS: list[tuple[float, bool]] = [
    (0.15, True),    # current: must also be structurally broken
    (0.15, False),   # drop the AND
    (0.20, False),
    (0.25, False),
    (0.30, False),
    (0.35, False),
]

HISTOGRAM_BINS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.70, 1.0]


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    try:
        lines: list[str] = []
        lines.append("# Quarantine probe")
        lines.append("")

        # ----- Current state by (status, reason) ---------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COALESCE(quarantine_reason, '∅'), COUNT(*)
                  FROM chunks WHERE is_current = true
                 GROUP BY status, quarantine_reason
                 ORDER BY 3 DESC
                """,
            )
            current = cur.fetchall()
        conn.commit()
        lines.append("## Current chunk classification")
        lines.append("")
        lines.append("| status | reason | count |")
        lines.append("| --- | --- | ---: |")
        total = 0
        for status, reason, n in current:
            lines.append(f"| {status} | {reason} | {n:,} |")
            total += int(n)
        lines.append(f"| | **total** | **{total:,}** |")
        lines.append("")

        # ----- Quality score histogram -------------------------------
        with conn.cursor(name="quality_load") as cur:
            cur.itersize = 5000
            cur.execute(
                """
                SELECT quality_score, quality_subscores, status
                  FROM chunks
                 WHERE is_current = true
                   AND quality_score IS NOT NULL
                """,
            )
            qs: list[float] = []
            dict_ratios: list[float] = []
            non_alpha: list[float] = []
            avg_word: list[float] = []
            status_by_idx: list[str] = []
            for q, subs, status in cur:
                qs.append(float(q))
                d = subs or {}
                if isinstance(d, str):
                    d = json.loads(d)
                dict_ratios.append(float(d.get("dict_word_ratio", 0.0)))
                non_alpha.append(float(d.get("non_alpha_ratio", 0.0)))
                avg_word.append(float(d.get("avg_word_len", 0.0)))
                status_by_idx.append(status)
        conn.commit()
        n_scored = len(qs)
        lines.append(f"## Quality scores ({n_scored:,} scored chunks)")
        lines.append("")
        if n_scored:
            lines.append(
                f"composite mean **{mean(qs):.3f}** · median **{median(qs):.3f}** · "
                f"min **{min(qs):.3f}** · max **{max(qs):.3f}**"
            )
            lines.append("")
            lines.append("### dict_word_ratio histogram")
            lines.append("")
            lines.append("| range | count | %active currently |")
            lines.append("| --- | ---: | ---: |")
            for lo, hi in zip(HISTOGRAM_BINS, HISTOGRAM_BINS[1:]):
                in_bin = [
                    (s == "active")
                    for d, s in zip(dict_ratios, status_by_idx)
                    if lo <= d < hi
                ]
                n = len(in_bin)
                pct = (sum(in_bin) / n * 100) if n else 0
                lines.append(f"| [{lo:.2f}, {hi:.2f}) | {n:,} | {pct:.0f}% |")
        lines.append("")

        # ----- Refused-label clusters --------------------------------
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, label_text, size
                  FROM clusters
                 WHERE run_id = (SELECT run_id FROM active_cluster_run WHERE singleton = true)
                   AND tier = 0
                   AND label_text IS NOT NULL
                """,
            )
            tier0 = cur.fetchall()
        conn.commit()

        refusal_labels: list[int] = []
        for _id, lab, text, _size in tier0:
            low = (text or "").lower()
            if any(n in low for n in REFUSAL_NEEDLES):
                refusal_labels.append(int(lab))

        lines.append(f"## Tier-0 clusters with refusal labels ({len(refusal_labels):,})")
        lines.append("")
        if refusal_labels:
            # For each, fetch chunks via chunk_projections + count active.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cp.cluster_t0, c.status, COUNT(*), AVG(c.quality_score)
                      FROM chunk_projections cp
                      JOIN chunks c ON c.id = cp.chunk_id
                     WHERE cp.run_id = (SELECT run_id FROM active_cluster_run WHERE singleton = true)
                       AND cp.cluster_t0 = ANY(%s)
                       AND c.is_current = true
                     GROUP BY cp.cluster_t0, c.status
                    """,
                    (refusal_labels,),
                )
                per_cluster: dict[int, dict] = {}
                for lab, status, n, q in cur:
                    d = per_cluster.setdefault(int(lab), {})
                    d[status] = {"count": int(n), "avg_q": float(q) if q is not None else 0.0}
            conn.commit()
            lines.append("| cluster | active | active avg_q | quarantined |")
            lines.append("| ---: | ---: | ---: | ---: |")
            total_active_in_refused = 0
            for lab in sorted(per_cluster):
                d = per_cluster[lab]
                act = d.get("active", {"count": 0, "avg_q": 0.0})
                qua = d.get("quarantined", {"count": 0, "avg_q": 0.0})
                total_active_in_refused += act["count"]
                lines.append(
                    f"| #{lab} | {act['count']:,} | {act['avg_q']:.3f} | {qua['count']:,} |"
                )
            lines.append(
                f"| **total** | **{total_active_in_refused:,}** | | |"
            )
            lines.append("")
            lines.append(
                f"**{total_active_in_refused:,} chunks live in clusters Haiku judged "
                "unreadable but currently have status='active'.** These are the chunks "
                "leaking into RAG retrieval."
            )
        lines.append("")

        # ----- Projected quarantine counts ---------------------------
        lines.append("## Projected quarantine counts under candidate bars")
        lines.append("")
        lines.append(
            "Probe-only — does NOT write to DB. Counts how many "
            "currently-active chunks would flip to quarantined under "
            "each candidate threshold."
        )
        lines.append("")
        lines.append("| dict_ratio < | require structural? | currently active → quarantine | % of active |")
        lines.append("| ---: | --- | ---: | ---: |")
        currently_active = sum(1 for s in status_by_idx if s == "active")
        for thresh, require_struct in CANDIDATE_THRESHOLDS:
            n_flip = 0
            for d, na, aw, s in zip(dict_ratios, non_alpha, avg_word, status_by_idx):
                if s != "active":
                    continue
                if d >= thresh:
                    continue
                if require_struct:
                    struct_broken = aw < 1.5 or aw > 18.0 or na > 0.6
                    if not struct_broken:
                        continue
                n_flip += 1
            pct = (n_flip / currently_active * 100) if currently_active else 0.0
            label = "yes (current)" if (thresh == 0.15 and require_struct) else "no"
            lines.append(
                f"| {thresh:.2f} | {label} | {n_flip:,} | {pct:.1f}% |"
            )
        lines.append("")

        out = "\n".join(lines)
        from pathlib import Path
        Path(SCRIPT_DIR_HINT).write_text(out)
        print(f"Wrote {SCRIPT_DIR_HINT}")
        print("\n" + out)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
