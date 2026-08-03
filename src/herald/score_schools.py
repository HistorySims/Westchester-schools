"""Quality / boilerplate scoring for the schools corpus.

The clustering and retrieval quality gate the newspaper engine had, rewritten
for governance documents (the `classify.py` FORK TODO). Two kinds of junk
dilute the map and pollute Ask evidence, and both get `status='quarantined'`:

1. **Garbled text** — genuinely unreadable chunks: PDF-extraction junk
   (mojibake, control-character soup) or the occasional badly-scanned
   attachment. Judged by the share of tokens that are *either* real words
   (English or Spanish) *or* clean numeric data — after stripping invisible
   control/bidi characters. So budget tables (numbers + account codes + terse
   labels) read as data and are kept — they're exactly the evidence the
   spending questions need — while only true symbol-soup fails. (This corpus is
   born-digital, so "bad OCR" was the wrong frame; garble is garble regardless.)
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
from pathlib import Path

import typer
from rich.console import Console

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

# Spanish counts as legible. Districts here (Elmsford, Port Chester, Ossining, …)
# publish full Spanish translations — mission statements, budget propositions,
# achievement data — which an English-only wordlist scores near-zero. Fold common
# Spanish words into the "known" set so those passages read as real, not garbled.
_ES_COMMON = frozenset(
    ["de", "la", "que", "el", "en", "y", "a", "los", "del", "se", "las", "por", "un", "para", "con", "no", "una", "su", "al", "es", "lo", "como", "más", "pero", "sus", "le", "ya", "o", "este", "sí", "porque", "esta", "entre", "cuando", "todo", "también", "fue", "había", "año", "años", "muy", "dos", "ser", "son", "hasta", "desde", "está", "están", "nuestra", "nuestro", "nuestros", "escuela", "escuelas", "distrito", "junta", "educación", "estudiantes", "estudiante", "estudiantil", "presupuesto", "escolar", "reunión", "propone", "suma", "impuestos", "misión", "visión", "aprendizaje", "enseñanza", "logro", "grados", "resultados", "matemáticas", "ciencias", "seguridad", "edificios", "votación", "papeleta", "comunidad", "familias", "maestros", "programa", "servicios", "reflejan", "mantener", "excelencia"]  # noqa: E501
)
# Invisible control / bidi / zero-width characters PDF extraction sometimes
# injects between words: they wreck naive scoring but the text reads fine once
# removed.
_CTRL_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]"
)
_MONEY_RE = re.compile(r"\$\s?\d")

# Known words: bundled English common list + Spanish common list. A real passage
# in either language clears the bar; genuine garble does not.
_EN_PATH = Path(__file__).parent / "wordlist.txt"


def _load_known() -> frozenset[str]:
    en: set[str] = set()
    if _EN_PATH.exists():
        en = {w.strip().lower() for w in _EN_PATH.read_text().splitlines() if w.strip()}
    return frozenset(en | set(_ES_COMMON))


_KNOWN = _load_known()

MIN_TOKENS_TO_JUDGE = 5   # fewer tokens than this = a fragment, judged elsewhere
MEANINGFUL_MIN = 0.25     # below this share of real words / data = garbled

# Well-formed data tokens: 9,200  3.26%  $35,702,827  (54,430)  1621.402.00.6012
_DATA_RE = re.compile(r"^[($]?-?[\d,]+(?:\.\d+)*\)?%?$")
_STRIP = ".,;:!?\"'()[]{}*/&%$#=+" + "\u2013\u2014\u2022"


def normalize(content: str) -> str:
    return _CTRL_RE.sub("", content)


def meaningful_fraction(content: str) -> float | None:
    """Share of tokens that are real words (EN/ES) or clean numeric data.

    Budget tables score high (numbers + codes are data); genuine symbol-soup
    scores near zero. Returns None for very short fragments (judged as
    too_short instead). This is what separates "garbled" from "data".
    """
    toks = normalize(content).split()
    if len(toks) < MIN_TOKENS_TO_JUDGE:
        return None
    good = 0
    for t in toks:
        if _DATA_RE.match(t):
            good += 1
            continue
        w = t.lower().strip(_STRIP)
        if len(w) >= 2 and w in _KNOWN:
            good += 1
    return good / len(toks)


@dataclass(frozen=True)
class ScoreResult:
    status: str            # 'active' | 'quarantined'
    reason: str | None     # 'garbled' | 'procedural' | 'too_short' | None
    quality_score: float   # [0,1]; lower = worse


def procedural_families(content: str) -> int:
    """How many distinct procedural families this chunk hits."""
    return sum(1 for pat in _PROC_FAMILIES.values() if pat.search(content))


def score_chunk(content: str) -> ScoreResult:
    """Quality/boilerplate verdict for one chunk."""
    word_count = len(normalize(content).split())
    mf = meaningful_fraction(content)   # None = too short to judge

    # 1) Garbled: mostly neither real words (EN/ES) nor clean numeric data —
    # i.e. symbol soup. A budget table scores high on data and is NOT garbled.
    if mf is not None and mf < MEANINGFUL_MIN:
        return ScoreResult("quarantined", "garbled", round(mf, 3))

    # 2) Fragments / headers-footers — but keep short $-lines (budget rows are
    # potential evidence for spending questions).
    if word_count < TOO_SHORT_WORDS and not _MONEY_RE.search(content):
        return ScoreResult("quarantined", "too_short", 0.2)

    # 3) Procedural boilerplate: short AND hits multiple families.
    families = procedural_families(content)
    if word_count <= PROC_MAX_WORDS and families >= PROC_MIN_FAMILIES:
        return ScoreResult("quarantined", "procedural", 0.2)

    # Active. quality_score from the meaningful fraction (neutral 0.6 for short
    # data rows), nudged down for single-family procedural chunks so ranking
    # prefers substance without dropping them.
    base = mf if mf is not None else 0.6
    penalty = 0.85 if families == 1 and word_count <= PROC_MAX_WORDS else 1.0
    return ScoreResult("active", None, round(min(1.0, base) * penalty, 3))


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Score schools chunks for readability + boilerplate.",
                  no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Group callback so ``run`` stays a named subcommand (room to grow)."""


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
