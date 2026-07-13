"""Corrective quarantine: any chunk in a tier-0 cluster Haiku refused
to label is unreadable for RAG purposes and gets status='quarantined'.

Haiku had access to 5+ representative chunks per cluster at labeling
time. When it answered with a refusal ("I cannot reliably identify a
shared topic — the text appears severely corrupted") it had real
evidence that the cluster's content is OCR garbage. We trust that
judgment: those chunks should not appear in RAG retrieval.

Idempotent — safe to re-run after each clustering / relabel pass.
Sets quarantine_reason='cluster_refused' so future audits can
distinguish these from heuristic-flagged quarantines. Refusal text
is matched with the same patterns the brief route uses to scrub the
UI (src/herald/recovery refusal list — kept in sync here).

Usage:
    uv run scripts/quarantine_by_cluster_refusal.py
"""

from __future__ import annotations

import re
import sys
from uuid import UUID

import psycopg

from herald import settings


# Same patterns the brief route scrubs from the UI plus a few more
# Haiku phrasings we've actually observed in cluster.label_text.
REFUSAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^i\s+cannot\b", re.IGNORECASE),
    re.compile(r"^i'?m\s+unable\b", re.IGNORECASE),
    re.compile(r"^unable\s+to\b", re.IGNORECASE),
    re.compile(r"\bocr[- ]?(damaged|corrupted|errors?)\b", re.IGNORECASE),
    re.compile(r"\bseverely\s+corrupted\b", re.IGNORECASE),
    re.compile(r"\bcannot\s+reliably\b", re.IGNORECASE),
    re.compile(r"\bunintelligible\b", re.IGNORECASE),
    re.compile(r"\bno\s+(clear|shared|coherent)\b", re.IGNORECASE),
    re.compile(r"^unclear\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+contain\b", re.IGNORECASE),
    re.compile(
        r"\bappears?\s+to\s+be\b.*\b(corrupted|damaged|garbled|improperly)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgarbled\s+or\s+improperly\s+formatted\b", re.IGNORECASE),
    re.compile(r"\bunable\s+to\s+(read|identify|determine)\b", re.IGNORECASE),
]


def is_refusal(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    # A label longer than ~200 chars is almost always a Haiku refusal
    # paragraph — real labels are 3-10 words.
    if len(t) > 200:
        return True
    return any(p.search(t) for p in REFUSAL_PATTERNS)


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    try:
        run_id = _active_run(conn)
        print(f"Active run: {run_id}")

        print("Loading tier-0 cluster labels...")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT label, label_text
                  FROM clusters
                 WHERE run_id = %s
                   AND tier = 0
                """,
                (run_id,),
            )
            rows = cur.fetchall()
        conn.commit()

        refused_labels: list[int] = []
        for label, text in rows:
            if is_refusal(text):
                refused_labels.append(int(label))

        print(f"  {len(rows):,} tier-0 clusters, "
              f"{len(refused_labels):,} flagged as refused")

        if not refused_labels:
            print("Nothing to do.")
            return

        print("Counting affected chunks (status='active' before update)...")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM chunks
                  JOIN chunk_projections cp ON cp.chunk_id = chunks.id
                 WHERE cp.run_id = %s
                   AND cp.cluster_t0 = ANY(%s)
                   AND chunks.status = 'active'
                   AND chunks.is_current = true
                """,
                (run_id, refused_labels),
            )
            n_before = int(cur.fetchone()[0])
        conn.commit()
        print(f"  {n_before:,} currently-active chunks live in refused clusters")

        if n_before == 0:
            print("Nothing to do.")
            return

        print("Applying quarantine...")
        with conn.transaction():
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE chunks
                   SET status = 'quarantined',
                       quarantined_at = COALESCE(quarantined_at, now()),
                       quarantine_reason = 'cluster_refused'
                  FROM chunk_projections cp
                 WHERE cp.chunk_id = chunks.id
                   AND cp.run_id = %s
                   AND cp.cluster_t0 = ANY(%s)
                   AND chunks.status = 'active'
                   AND chunks.is_current = true
                """,
                (run_id, refused_labels),
            )
            updated = cur.rowcount
        print(f"  Quarantined {updated:,} chunks.")

        # Confirmation read-back so the workflow log shows the new state.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*)
                  FROM chunks
                 WHERE is_current = true
                 GROUP BY status
                 ORDER BY 2 DESC
                """
            )
            for s, n in cur.fetchall():
                print(f"  status={s}: {int(n):,}")
        conn.commit()

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


if __name__ == "__main__":
    main()
