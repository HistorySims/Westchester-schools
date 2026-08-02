"""Quality / boilerplate scoring for the schools corpus.

The clustering and retrieval quality gate the newspaper engine had, rewritten
for governance documents (the `classify.py` FORK TODO). Two kinds of junk
dilute the map and pollute Ask evidence, and both get `status='quarantined'`:

1. **Illegible OCR** — scanned pages that didn't transcribe. Reuses the
   corpus-agnostic OCR legibility scorer from ``herald.classify``.
2. **Procedural boilerplate** — roll calls, motions ("moved by X, seconded by
   Y, carried 5-0"), minutes approvals, adjournments, public-comment notices.
   This text is near-identical across every district and topic, so it either
   forms one meaningless blob or scatters into the noise bin. It's also never
   useful *evidence*, so quarantining it helps Ask too.

Both the map (`load_chunks`) and Ask retrieval already filter
``status='active'``, so flipping junk to ``quarantined`` cleans both surfaces
at once. It's a reversible status flip — re-score to change the bar.

Conservative by design: procedural quarantine requires a *short* chunk that
hits *multiple* procedural families, so a substantive passage that merely
mentions "motion" or "budget" stays active.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import typer
from rich.console import Console

from herald.classify import classify_quality, compute_quality_scores

console = Console()

# Procedural-language families. A chunk dominated by several of these — and
# short — is meeting scaffolding, not substance.
_PROC_FAMILIES: dict[str, re.Pattern[str]] = {
    "rollcall": re.compile(
        r"\broll\s*call\b|\bmembers?\s+present\b|\b(present|absent)\s*[:\-—]",
        re.IGNORECASE,
    ),
    "motion": re.compile(
        r"\bmotion\s+(by|to|was|made|carried)\b|\bseconded\s+by\b|\bmoved\s+by\b"
        r"|\bupon\s+a\s+motion\b|\ball\s+in\s+favor\b|\bcarried\s+unanimously\b"
        r"|\bayes?\s*[:\-—]|\bnays?\s*[:\-—]|\babstentions?\b",
        re.IGNORECASE,
    ),
    "minutes": re.compile(
        r"\bapproval\s+of\s+(the\s+)?minutes\b|\bminutes\s+of\s+the\b[^.]{0,40}\bmeeting\b"
        r"|\bconsent\s+agenda\b",
        re.IGNORECASE,
    ),
    "logistics": re.compile(
        r"\bcall\s+to\s+order\b|\badjourn(ed|ment|s)?\b|\bpledge\s+of\s+allegiance\b"
        r"|\bexecutive\s+session\b|\bpublic\s+(comment|participation)\b"
        r"|\bnext\s+(regular\s+)?(meeting|board\s+meeting)\b|\bpoint\s+of\s+(order|information)\b",
        re.IGNORECASE,
    ),
}

# Thresholds (tunable). Procedural quarantine only fires on shortish chunks
# that hit multiple families — long substantive passages are safe.
PROC_MAX_WORDS = 180
PROC_MIN_FAMILIES = 2
TOO_SHORT_WORDS = 8


@dataclass(frozen=True)
class ScoreResult:
    status: str            # 'active' | 'quarantined'
    reason: str | None     # e.g. 'ocr_illegible', 'procedural', 'too_short'
    quality_score: float   # [0,1]; lower = worse


def procedural_families(content: str) -> int:
    """How many distinct procedural families this chunk hits."""
    return sum(1 for pat in _PROC_FAMILIES.values() if pat.search(content))


def score_chunk(content: str) -> ScoreResult:
    """Quality/boilerplate verdict for one chunk."""
    scores = compute_quality_scores(content)
    word_count = scores.word_count
    base_quality = scores.composite()

    # 1) OCR legibility (unconditional floors live in classify_quality).
    status, reason = classify_quality(scores)
    if status == "quarantined":
        return ScoreResult("quarantined", reason, base_quality)

    # 2) Fragments / headers-footers.
    if word_count < TOO_SHORT_WORDS:
        return ScoreResult("quarantined", "too_short", base_quality)

    # 3) Procedural boilerplate: short AND hits multiple families.
    families = procedural_families(content)
    if word_count <= PROC_MAX_WORDS and families >= PROC_MIN_FAMILIES:
        # scale quality down by how procedural it is
        return ScoreResult("quarantined", "procedural", base_quality * 0.4)

    # Active. Nudge quality down a little for single-family procedural chunks
    # so downstream ranking can prefer substance without dropping them.
    q = base_quality * (0.85 if families == 1 and word_count <= PROC_MAX_WORDS else 1.0)
    return ScoreResult("active", reason, q)


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Score schools chunks for OCR legibility + boilerplate.")


@app.command()
def run(
    dry_run: bool = typer.Option(
        False, "--dry-run/--write",
        help="Report what would change (with samples) without writing."
    ),
    rescore_all: bool = typer.Option(
        False, "--rescore-all",
        help="Re-score every chunk (default: only currently-active ones)."
    ),
    batch: int = typer.Option(2000, help="DB update batch size."),
    samples: int = typer.Option(6, help="Sample quarantined chunks per reason to print."),
) -> None:
    """Scan chunks, score them, and set status/quality_score."""
    from collections import defaultdict

    from herald import schools_db

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")

    where = "" if rescore_all else "where status = 'active'"
    counts: dict[str, int] = defaultdict(int)
    sample_rows: dict[str, list[str]] = defaultdict(list)
    updates: list[tuple[str, float, str]] = []  # (status, quality, id)
    total = 0

    with schools_db.connect(db_url) as conn:
        cur = conn.cursor()
        cur.execute(f"select id, content from chunks {where}")
        rows = cur.fetchall()
        console.print(f"scoring {len(rows)} chunks{' (all)' if rescore_all else ' (active)'}")
        for cid, content in rows:
            total += 1
            res = score_chunk(content or "")
            counts[res.status] += 1
            if res.status == "quarantined":
                counts[f"  reason:{res.reason}"] += 1
                if len(sample_rows[res.reason or "?"]) < samples:
                    sample_rows[res.reason or "?"].append((content or "")[:160].replace("\n", " "))
            updates.append((res.status, round(res.quality_score, 4), str(cid)))

        console.print(f"\n[bold]verdict over {total} chunks[/bold]")
        for k in sorted(counts):
            console.print(f"  {k}: {counts[k]}")
        q = counts.get("quarantined", 0)
        console.print(f"\n[bold]{q} → quarantined[/bold] ({100*q/max(total,1):.1f}%), "
                      f"{counts.get('active',0)} stay active")
        for reason, examples in sample_rows.items():
            console.print(f"\n[yellow]sample {reason}[/yellow]:")
            for ex in examples:
                console.print(f"  · {ex}")

        if dry_run:
            console.print("\n[yellow]dry run — no changes written[/yellow]")
            return

        cur.execute(
            "create temp table _score (status text, quality_score real, id uuid) on commit drop"
        )
        with cur.copy("copy _score (status, quality_score, id) from stdin") as copy:
            for row in updates:
                copy.write_row(row)
        cur.execute(
            "update chunks c set status = s.status, quality_score = s.quality_score "
            "from _score s where c.id = s.id"
        )
        conn.commit()
        console.print(f"\n[green]wrote[/green] status/quality_score for {len(updates)} chunks")


if __name__ == "__main__":
    app()
