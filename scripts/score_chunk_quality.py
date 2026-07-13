"""Backfill OCR quality scores + quarantine status for every chunk.

Idempotent. Safe to run after every ingest and after Phase-3 re-OCR.

Usage:
    uv run scripts/score_chunk_quality.py
    # or:
    python scripts/score_chunk_quality.py

Requires: SUPABASE_DB_URL in env or .env, and the 0006_chunk_quality
migration already applied.

Reads:  chunks.content for every is_current = true row.
Writes: chunks.status / quality_score / quality_subscores /
        quarantined_at / quarantine_reason.

No re-embedding, no LLM calls. Pure heuristic.
"""

from __future__ import annotations

import sys

from herald import settings
from herald.quality import score_all


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    print("Scoring chunk OCR quality (heuristic, no API calls)...")
    summary = score_all(cfg.supabase_db_url, on_progress=print)

    print()
    print(f"  total scored:              {summary.total:,}")
    print(f"  quarantined:               {summary.quarantined:,}")
    print(f"  reassignment candidates:   {summary.reassignment_candidates:,}")
    print(f"  clean active:              {summary.active_clean:,}")
    if summary.quarantined_reasons:
        print("  quarantine reasons:")
        for reason, n in sorted(
            summary.quarantined_reasons.items(), key=lambda kv: -kv[1]
        ):
            print(f"    {reason}: {n:,}")


if __name__ == "__main__":
    main()
