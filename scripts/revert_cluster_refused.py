"""Revert the cluster_refused quarantine pass.

Run BEFORE recomputing geometry + relabeling, after a fresh per-chunk
score-quality pass. The cluster_refused corrective was applied using
Haiku labels generated from the pre-quarantine corpus, so some of the
750 chunks it captured may have been individually readable — the
cluster's rep-chunk samples happened to be the worst members, Haiku
refused, and the whole cluster's membership got swept in.

This script re-evaluates every cluster_refused chunk against the
per-chunk heuristic using its already-stored quality_subscores:

  - If the per-chunk heuristic says quarantine (typically
    'ocr_illegible'), the chunk stays quarantined under the new
    reason. We're not pretending it's recoverable — it isn't.
  - If the per-chunk heuristic says active, the chunk goes back to
    active. After recompute + relabel, the cluster_refused corrective
    can be re-applied; clusters Haiku STILL refuses after the cleanup
    get cluster_refused again, but only those.

Idempotent — re-running after a re-applied cluster_refused pass is a
no-op for newly-quarantined chunks (their reason is fresh, not stale).

Usage:
    uv run scripts/revert_cluster_refused.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import psycopg

from herald import settings
from herald.classify import QualityScores, classify_quality


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Report counts; do not write to the database.")
    args = parser.parse_args()

    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    try:
        # Pull cluster_refused chunks with their stored subscores.
        print("Loading cluster_refused chunks...")
        with conn.cursor(name="revert_load") as cur:
            cur.itersize = 5000
            cur.execute(
                """
                SELECT id, quality_subscores
                  FROM chunks
                 WHERE is_current = true
                   AND status = 'quarantined'
                   AND quarantine_reason = 'cluster_refused'
                """,
            )
            rows = cur.fetchall()
        conn.commit()
        print(f"  {len(rows):,} candidates to reconsider")
        if not rows:
            print("Nothing to do.")
            return

        # Per-chunk verdict on each. Buckets:
        #   stays_quarantined: per-chunk heuristic says quarantine
        #   reactivated:       per-chunk heuristic says active
        updates: list[tuple] = []
        n_stays = 0
        n_active_clean = 0
        n_reassign = 0
        n_no_scores = 0
        now = datetime.now(timezone.utc)

        for chunk_id, subscores in rows:
            if not subscores:
                # No stored quality scores — leave the chunk quarantined
                # under a clear reason so it doesn't silently slip back.
                n_no_scores += 1
                updates.append((
                    "quarantined", now, "ocr_illegible_unverified", str(chunk_id),
                ))
                n_stays += 1
                continue

            d = subscores if isinstance(subscores, dict) else json.loads(subscores)
            scores = QualityScores(
                non_alpha_ratio=float(d.get("non_alpha_ratio", 1.0)),
                avg_word_len=float(d.get("avg_word_len", 0.0)),
                dict_word_ratio=float(d.get("dict_word_ratio", 0.0)),
                word_count=int(d.get("word_count", 0)),
            )
            status, reason = classify_quality(scores)

            if status == "quarantined":
                # Per-chunk heuristic agrees the chunk is bad. Keep it
                # quarantined under the heuristic's reason
                # ('ocr_illegible' or 'too_short') so it's no longer
                # marked cluster_refused.
                updates.append((status, now, reason, str(chunk_id)))
                n_stays += 1
            else:
                # Per-chunk heuristic says active. Restore to active
                # with the heuristic's reason ('reassignment_candidate'
                # or NULL). quarantined_at gets cleared.
                updates.append((status, None, reason, str(chunk_id)))
                if reason == "reassignment_candidate":
                    n_reassign += 1
                else:
                    n_active_clean += 1

        print()
        print(f"  stays quarantined (per-chunk heuristic also says bad): {n_stays:,}")
        print(f"      of which had no stored quality scores: {n_no_scores:,}")
        print(f"  → active 'reassignment_candidate': {n_reassign:,}")
        print(f"  → active clean: {n_active_clean:,}")
        recovered = n_reassign + n_active_clean
        print(f"  total recovered to active: {recovered:,} "
              f"({recovered / len(rows) * 100:.1f}% of cluster_refused)")

        if args.dry_run:
            print("\nDRY RUN — no writes performed.")
            return

        print("\nApplying updates...")
        with conn.transaction():
            cur = conn.cursor()
            cur.executemany(
                """
                UPDATE chunks
                   SET status = %s,
                       quarantined_at = %s,
                       quarantine_reason = %s
                 WHERE id = %s::uuid
                """,
                updates,
            )

        # Confirmation read-back.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*)
                  FROM chunks
                 WHERE is_current = true
                 GROUP BY status
                 ORDER BY 2 DESC
                """,
            )
            for s, n in cur.fetchall():
                print(f"  status={s}: {int(n):,}")
        conn.commit()
        print("Done.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
