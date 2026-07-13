"""Populate cluster_weeks: per-cluster per-week aggregates for the
Cluster Dossier page.

For every (tier, label) cluster in the active run, bins its
status='active' AND content_type=0 member chunks by ISO week and
computes per week:

  chunk_count       — members that week
  count_by_paper    — {lccn: count}
  mean_ocr_quality  — mean quality_score of scored members (null if none)
  centroid_x/_y     — mean of members' stored UMAP coords
                      (mean-of-projections, deliberately — no UMAP
                      model persistence, visually consistent with the
                      explore map)
  top_terms         — top ~5 c-TF-IDF terms for the week, computed
                      WITHIN the cluster: this week's chunks vs the
                      cluster's other weeks. Pure term math, no LLM.

Idempotent — deletes and rewrites all rows for the active run's
clusters. Run after cluster_recompute.py.

Usage:
    uv run scripts/cluster_weeks.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from math import log
from uuid import UUID

import psycopg

from schoolsengine import settings


TOP_TERMS_PER_WEEK = 5
MIN_TERM_COUNT = 2          # a term must appear ≥2× in the week to qualify
TOKEN_RE = re.compile(r"[a-z]{3,}")

# Compact English stopword list plus 1840s newspaper boilerplate.
# Deliberately small — c-TF-IDF's within-cluster IDF already crushes
# terms that appear in every week of the story.
STOPWORDS = frozenset("""
the and for that with was his this have are not but from they her she
him has had who which were been their there will would all one two when
what then them out our your you can could should may might must shall
upon said say says very more most much many some any each every other
into over under after before about above between both same such only
also being its these those does did done because where while against
during without within through however therefore thus hence yet still
nor own too here now even ever never again once just like well make
made take taken give given get got see seen know known come came went
gone way day days man men mrs miss esq inst ult new old
""".split())


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)

    try:
        run_id = _active_run(conn)
        print(f"Active run: {run_id}")

        print("Loading cluster id map...")
        cluster_ids = _load_cluster_ids(conn, run_id)
        print(f"  {len(cluster_ids):,} clusters")

        print("Loading active+content chunks...")
        rows = _load_rows(conn, run_id)
        print(f"  {len(rows):,} chunks")
        if not rows:
            print("Nothing to compute.")
            return

        print("Computing per-cluster weekly aggregates + c-TF-IDF terms...")
        week_rows = _compute(rows, cluster_ids)
        print(f"  {len(week_rows):,} (cluster, week) rows")

        print("Writing cluster_weeks...")
        n = _write(conn, run_id, week_rows)
        print(f"  Inserted {n:,} rows.")

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


def _load_cluster_ids(
    conn: psycopg.Connection, run_id: UUID,
) -> dict[tuple[int, int], UUID]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, label, id FROM clusters WHERE run_id = %s",
            (run_id,),
        )
        out = {
            (int(r[0]), int(r[1])): (r[2] if isinstance(r[2], UUID) else UUID(str(r[2])))
            for r in cur.fetchall()
        }
    conn.commit()
    return out


def _load_rows(
    conn: psycopg.Connection, run_id: UUID,
) -> list[tuple[date, float, float, float | None, str, str, int, int, int, int]]:
    """(date, x, y, quality, lccn, content, t0, t1, t2, t3) per chunk."""
    out: list = []
    with conn.cursor(name="weeks_load_chunks") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT issues.date_issued,
                   chunk_projections.x,
                   chunk_projections.y,
                   chunks.quality_score,
                   papers.lccn,
                   chunks.content,
                   chunk_projections.cluster_t0,
                   chunk_projections.cluster_t1,
                   chunk_projections.cluster_t2,
                   chunk_projections.cluster_t3
            FROM chunk_projections
            JOIN chunks ON chunks.id = chunk_projections.chunk_id
            JOIN pages  ON pages.id = chunks.page_id
            JOIN issues ON issues.id = pages.issue_id
            JOIN papers ON papers.id = issues.paper_id
            WHERE chunk_projections.run_id = %s
              AND chunks.status = 'active'
              AND chunk_projections.content_type = 0
            """,
            (run_id,),
        )
        for r in cur:
            out.append((
                r[0], float(r[1]), float(r[2]),
                float(r[3]) if r[3] is not None else None,
                r[4], r[5],
                int(r[6]), int(r[7]), int(r[8]), int(r[9]),
            ))
    conn.commit()
    return out


def _week_start(d: date) -> date:
    """Monday of the ISO week — matches the web's isoWeekStart."""
    return d - timedelta(days=d.isoweekday() - 1)


def _tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def _compute(
    rows: list,
    cluster_ids: dict[tuple[int, int], UUID],
) -> list[dict]:
    # Bucket chunk indices per (tier, label).
    members: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        t0, t1, t2, t3 = r[6], r[7], r[8], r[9]
        for tier, lab in ((0, t0), (1, t1), (2, t2), (3, t3)):
            if lab < 0:
                continue
            if (tier, lab) not in cluster_ids:
                continue
            members[(tier, lab)].append(idx)

    out: list[dict] = []
    for key, idxs in members.items():
        cluster_id = cluster_ids[key]

        # Bin by week.
        by_week: dict[date, list[int]] = defaultdict(list)
        for i in idxs:
            by_week[_week_start(rows[i][0])].append(i)
        weeks_sorted = sorted(by_week.keys())

        # Token counts per week, for within-cluster c-TF-IDF.
        week_tokens: dict[date, Counter] = {}
        for w in weeks_sorted:
            c: Counter = Counter()
            for i in by_week[w]:
                c.update(_tokenize(rows[i][5]))
            week_tokens[w] = c

        # c-TF-IDF (BERTopic-style): score(t, w) = count(t, w) *
        # log(1 + A / f(t)), where A = mean tokens per week and f(t) =
        # total count of t across the cluster's weeks. A term that
        # appears every week scores near zero; a term concentrated in
        # one week scores high.
        total_per_term: Counter = Counter()
        total_tokens = 0
        for c in week_tokens.values():
            total_per_term.update(c)
            total_tokens += sum(c.values())
        avg_tokens_per_week = total_tokens / max(1, len(weeks_sorted))

        for w in weeks_sorted:
            members_w = by_week[w]
            counts: Counter = Counter()
            qualities: list[float] = []
            xs: list[float] = []
            ys: list[float] = []
            for i in members_w:
                counts[rows[i][4]] += 1
                if rows[i][3] is not None:
                    qualities.append(rows[i][3])
                xs.append(rows[i][1])
                ys.append(rows[i][2])

            scored = [
                (cnt * log(1.0 + avg_tokens_per_week / total_per_term[t]), t)
                for t, cnt in week_tokens[w].items()
                if cnt >= MIN_TERM_COUNT
            ]
            scored.sort(reverse=True)
            top_terms = [t for _s, t in scored[:TOP_TERMS_PER_WEEK]]

            out.append({
                "cluster_id": cluster_id,
                "week_start": w,
                "chunk_count": len(members_w),
                "count_by_paper": dict(counts),
                "mean_ocr_quality":
                    sum(qualities) / len(qualities) if qualities else None,
                "centroid_x": sum(xs) / len(xs),
                "centroid_y": sum(ys) / len(ys),
                "top_terms": top_terms,
            })
    return out


def _write(conn: psycopg.Connection, run_id: UUID, week_rows: list[dict]) -> int:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM cluster_weeks
             WHERE cluster_id IN (SELECT id FROM clusters WHERE run_id = %s)
            """,
            (run_id,),
        )
        batch = 500
        for start in range(0, len(week_rows), batch):
            chunk = week_rows[start:start + batch]
            cur.executemany(
                """
                INSERT INTO cluster_weeks
                  (cluster_id, week_start, chunk_count, count_by_paper,
                   mean_ocr_quality, centroid_x, centroid_y, top_terms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        r["cluster_id"], r["week_start"], r["chunk_count"],
                        json.dumps(r["count_by_paper"]),
                        r["mean_ocr_quality"],
                        r["centroid_x"], r["centroid_y"],
                        json.dumps(r["top_terms"]),
                    )
                    for r in chunk
                ],
            )
    return len(week_rows)


if __name__ == "__main__":
    main()
