"""GATE 2 — Build the recovery-eval test set + baseline the heuristic.

Only runs because Gate 1 passed: both our papers are fully present in
the American Stories 1845 subset, so clean layout-aware OCR exists
for (nearly) every page our quarantined chunks live on.

Steps (all read-only against our DB; AS download is 1845-only):

  4. JOIN   quarantined chunks ↔ AS clean page text, by LCCN + date +
            page. Two schema variants and occasional page_number="na"
            were found at Gate 1, so the join is defensive and falls
            back to date-level + fragment disambiguation.
  5. LABEL  each joined chunk for the test theme "political violence"
            from the CLEAN text. Region-level where our surviving
            fragments align to a specific AS region; page-level
            otherwise. Lexicon first; Haiku only for ambiguous cases,
            HARD-CAPPED at 100 calls (skipped entirely if no API key).
  6. SPLIT  tune 70% / holdout 30%, fixed seed, separate files.
  7. BASELINE  score the garbled chunks with the CURRENT heuristic in
            its question-scoped form (theme-conditioned components,
            current weights) and compute precision@15/@20 + recall on
            TUNE and HOLDOUT.

Adjustments vs the original driving prompt (agreed at review):
  - The baseline is the THEME-SCOPED composite, not the global
    recovery_value — a global "is this worth attention" score judged
    against a theme answer key would conflate two questions.
  - Labels are region-level where fragment↔region alignment is
    confident, page-level otherwise, with the method recorded.
  - Refused clusters (label_text is a Haiku refusal) are never used
    as theme references.

Outputs (uploaded as workflow artifacts):
  scripts/baseline_eval.md      — the Gate 2 report for human review
  scripts/recovery_eval_tune.jsonl
  scripts/recovery_eval_holdout.jsonl

GATE 2 STOP: Phase 3 (tuning) does not start until the human has
reviewed baseline_eval.md. Determinism note: the split uses SEED on
sorted chunk ids, so Phase 3 can re-derive the same tune/holdout sets
by re-running this script (Haiku-labeled ambiguous cases may vary
slightly between runs; their count is reported).

Usage:
    uv run --with "datasets<3" python scripts/american_stories_gate2.py
"""

from __future__ import annotations

import difflib
import json
import random
import re
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# Reuse the live heuristic's constants + helpers so the baseline is
# honestly "the current heuristic, question-scoped" — not a rewrite.
from recovery_score import (  # noqa: E402
    GAP_BONUS,
    PROXIMITY_QUALITY_FLOOR,
    W_COMMERCIALITY,
    W_ENTITY,
    W_FOOTPRINT,
    W_GRID,
    W_PROXIMITY,
    W_RECOVER,
    classify_commercial,
    damage_variants,
)
from quarantine_by_cluster_refusal import is_refusal  # noqa: E402

from herald import settings  # noqa: E402


REPORT_PATH = SCRIPT_DIR / "baseline_eval.md"
TUNE_PATH = SCRIPT_DIR / "recovery_eval_tune.jsonl"
HOLDOUT_PATH = SCRIPT_DIR / "recovery_eval_holdout.jsonl"

TARGET_LCCNS = ("sn83030213", "sn83030313")

# ---- Tunable constants -------------------------------------------------

SEED = 42
TUNE_FRACTION = 0.70
P_AT = (15, 20)

HAIKU_CAP = 100
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Fragment→region alignment: a chunk maps to a specific AS region when
# at least ALIGN_MIN_HITS of its distinctive fragments appear in the
# region text and the hit fraction is ≥ ALIGN_MIN_FRACTION.
ALIGN_MIN_HITS = 3
ALIGN_MIN_FRACTION = 0.25
ALIGN_MAX_FRAGMENTS = 30      # use up to N distinctive fragments per chunk

PAGE_TEXT_TRIM = 4000          # clean-text cap stored per eval row
FUZZY_LEX_MIN = 0.80           # difflib ratio floor for fragment↔lexicon

# ---- The test theme: political violence --------------------------------
# STRONG terms: one hit on clean text labels the page/region positive.
# WEAK terms: suggestive but generic; hits make a case "ambiguous"
# (→ Haiku, capped) rather than positive.
STRONG_TERMS = (
    "anti-rent", "anti rent", "antirent", "anti-renters", "down-rent",
    "riot", "riots", "rioters", "mob", "insurrection", "insurgents",
    "affray", "posse", "lynch", "outrage",
    "disguised as indians", "calico indians",
    "distress warrant",
)
WEAK_TERMS = (
    "murder", "murdered", "assault", "assassination", "sheriff",
    "tenant", "tenants", "landlord", "militia", "armed resistance",
    "rebellion", "arson", "incendiary", "treason", "disguised",
)
# Entities strongly tied to the Anti-Rent violence in this period.
THEME_ENTITIES = (
    "boughton", "steele", "van rensselaer", "rensselaer", "andes",
    "delhi", "delaware county", "osman", "earle", "wright",
)
# Cluster labels containing any of these select the THEME clusters used
# for proximity / footprint / grid / gap conditioning.
THEME_CLUSTER_KEYWORDS = (
    "anti-rent", "anti rent", "antirent", "riot", "mob", "insurrection",
    "violence", "murder", "posse", "tenant", "affray", "militia",
    "sheriff", "down-rent",
)


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    lines: list[str] = []
    lines.append("# Gate 2 — eval set + theme-scoped baseline")
    lines.append("")
    lines.append(f"Theme: **political violence** · seed={SEED} · "
                 f"tune={TUNE_FRACTION:.0%}")
    lines.append("")

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False,
                           prepare_threshold=None)
    register_vector(conn)
    try:
        # ---- Our side of the join ------------------------------------
        print("Loading quarantined chunks + fragments + components from DB...")
        chunks = _load_quarantined(conn)
        fragments = _load_fragments(conn)
        clusters = _load_clusters(conn)
        print(f"  {len(chunks):,} quarantined chunks, "
              f"{len(clusters):,} tier-0 clusters")

        # ---- Theme conditioning sets ---------------------------------
        theme_clusters, commercial_clusters, refused_clusters = \
            _select_theme_clusters(clusters)
        lines.extend(_theme_report(clusters, theme_clusters,
                                   commercial_clusters, refused_clusters))

        # ---- AS side of the join -------------------------------------
        print("Loading American Stories 1845 subset (our papers only)...")
        wanted_pages = {(c["lccn"], c["date"]) for c in chunks.values()}
        as_pages, load_note = _load_as_pages(wanted_pages)
        lines.append("## Dataset load")
        lines.append("")
        lines.append(load_note)
        lines.append("")
        if as_pages is None:
            lines.append("**GATE 2 ABORTED — dataset inaccessible.**")
            _write_report(lines)
            return
        print(f"  {len(as_pages):,} AS page records matched our "
              f"(lccn, date) universe")

        # ---- Step 4: JOIN --------------------------------------------
        print("Joining chunks to clean AS text (page + region alignment)...")
        eval_rows, join_stats = _join(chunks, fragments, as_pages)
        lines.extend(_join_report(join_stats, len(chunks)))
        if len(eval_rows) < 50:
            lines.append("")
            lines.append(f"**⚠ EVAL SET IS SMALL ({len(eval_rows)} chunks)** — "
                         "this limits everything downstream. Flagging "
                         "prominently per the gate rules.")
            lines.append("")

        # ---- Step 5: LABEL -------------------------------------------
        print("Labeling from clean text (lexicon first, Haiku capped)...")
        label_stats = _label(eval_rows)
        lines.extend(_label_report(label_stats, eval_rows))

        # ---- Step 6: SPLIT -------------------------------------------
        tune, holdout = _split(eval_rows)
        lines.append("## Tune / holdout split")
        lines.append("")
        lines.append(f"- TUNE: **{len(tune):,}** chunks "
                     f"({sum(r['label'] for r in tune):,} positive)")
        lines.append(f"- HOLDOUT: **{len(holdout):,}** chunks "
                     f"({sum(r['label'] for r in holdout):,} positive)")
        lines.append(f"- Split: sorted chunk ids shuffled with seed {SEED}; "
                     "separate JSONL files (structural separation).")
        lines.append("")

        # ---- Step 7: BASELINE ----------------------------------------
        print("Scoring garbled chunks with the theme-scoped baseline...")
        _score_theme_baseline(
            conn, eval_rows, theme_clusters, commercial_clusters, clusters,
        )
        lines.extend(_baseline_report(tune, holdout))

        # ---- Persist eval set ----------------------------------------
        _write_jsonl(TUNE_PATH, tune)
        _write_jsonl(HOLDOUT_PATH, holdout)
        lines.append("## Files")
        lines.append("")
        lines.append(f"- `{TUNE_PATH.name}` / `{HOLDOUT_PATH.name}` — one JSON "
                     "object per chunk: ids, join method, label + method, "
                     "clean-text snippet, component scores, theme score.")
        lines.append("")
        lines.append("**GATE 2 STOP — awaiting human review before Phase 3.**")

        _write_report(lines)
    finally:
        conn.close()


# ======================= DB loaders =====================================


def _load_quarantined(conn) -> dict[str, dict]:
    """chunk_id(str) → row dict with everything the baseline needs."""
    out: dict[str, dict] = {}
    with conn.cursor(name="g2_chunks") as cur:
        cur.itersize = 2000
        cur.execute(
            """
            SELECT chunks.id, chunks.content, chunks.quality_score,
                   chunks.embedding,
                   papers.lccn, issues.date_issued, issues.edition,
                   pages.sequence,
                   cp.cluster_t0,
                   cr.grid_section_guess, cr.grid_confidence,
                   cr.footprint_cluster_label, cr.footprint_score,
                   gap_cl.label AS gap_label
              FROM chunks
              JOIN pages  ON pages.id = chunks.page_id
              JOIN issues ON issues.id = pages.issue_id
              JOIN papers ON papers.id = issues.paper_id
              LEFT JOIN chunk_projections cp
                     ON cp.chunk_id = chunks.id
                    AND cp.run_id = (SELECT run_id FROM active_cluster_run
                                     WHERE singleton = true)
              LEFT JOIN chunk_recovery cr ON cr.chunk_id = chunks.id
              LEFT JOIN clusters gap_cl ON gap_cl.id = cr.gap_candidate_cluster_id
             WHERE chunks.status = 'quarantined'
               AND chunks.is_current = true
            """
        )
        for r in cur:
            cid = str(r[0])
            out[cid] = {
                "chunk_id": cid,
                "content": r[1] or "",
                "quality": float(r[2]) if r[2] is not None else 0.0,
                "embedding": (np.asarray(r[3], dtype=np.float32)
                              if r[3] is not None else None),
                "lccn": r[4],
                "date": r[5].isoformat(),
                "edition": int(r[6]),
                "sequence": int(r[7]),
                "cluster_t0": int(r[8]) if r[8] is not None else -1,
                "grid_guess": int(r[9]) if r[9] is not None else None,
                "grid_conf": float(r[10]) if r[10] is not None else 0.0,
                "footprint_label": int(r[11]) if r[11] is not None else None,
                "footprint_score": float(r[12]) if r[12] is not None else 0.0,
                "gap_label": int(r[13]) if r[13] is not None else None,
            }
    conn.commit()
    return out


def _load_fragments(conn) -> dict[str, list[tuple[str, str]]]:
    """chunk_id → [(fragment, kind)], capital-kind first (distinctive)."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with conn.cursor(name="g2_frags") as cur:
        cur.itersize = 5000
        cur.execute(
            "SELECT chunk_id, fragment, kind FROM quarantine_fragments"
        )
        for chunk_id, frag, kind in cur:
            out[str(chunk_id)].append((frag, kind))
    conn.commit()
    for cid in out:
        out[cid].sort(key=lambda fk: (fk[1] != "capital", fk[0]))
    return dict(out)


def _load_clusters(conn) -> dict[int, dict]:
    """tier-0 clusters of the active run: label → {label_text, centroid}."""
    out: dict[int, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, label_text, active_centroid, centroid
              FROM clusters
             WHERE run_id = (SELECT run_id FROM active_cluster_run
                             WHERE singleton = true)
               AND tier = 0
            """
        )
        for label, text, active_c, fallback_c in cur:
            vec = active_c if active_c is not None else fallback_c
            out[int(label)] = {
                "label_text": text,
                "centroid": (np.asarray(vec, dtype=np.float32)
                             if vec is not None else None),
            }
    conn.commit()
    return out


def _select_theme_clusters(
    clusters: dict[int, dict],
) -> tuple[set[int], set[int], set[int]]:
    refused = {
        lab for lab, c in clusters.items() if is_refusal(c["label_text"])
    }
    labels_by_t0 = {
        lab: c["label_text"] for lab, c in clusters.items()
        if c["label_text"]
    }
    commercial = classify_commercial(labels_by_t0) - refused
    theme: set[int] = set()
    for lab, c in clusters.items():
        if lab in refused or not c["label_text"]:
            continue
        low = c["label_text"].lower()
        if any(kw in low for kw in THEME_CLUSTER_KEYWORDS):
            theme.add(lab)
    return theme, commercial, refused


def _theme_report(clusters, theme, commercial, refused) -> list[str]:
    lines = ["## Theme conditioning", ""]
    lines.append(f"Theme clusters selected by label keywords "
                 f"({len(theme)}); refused clusters excluded ({len(refused)}); "
                 f"commercial clusters ({len(commercial)}) contribute "
                 "negative commerciality and are ineligible for gap bonus.")
    lines.append("")
    for lab in sorted(theme):
        lines.append(f"- theme #{lab} — {clusters[lab]['label_text']}")
    if not theme:
        lines.append("- _(no cluster labels matched the theme keywords — "
                     "proximity/footprint/grid/gap conditioning will "
                     "contribute nothing; the lexicon fragment signal "
                     "carries the baseline)_")
    lines.append("")
    lines.append("Lexicon (STRONG → positive on hit; WEAK → ambiguous → "
                 "Haiku): see constants in the script. Strong: "
                 + ", ".join(STRONG_TERMS) + ". Weak: "
                 + ", ".join(WEAK_TERMS) + ". Theme entities: "
                 + ", ".join(THEME_ENTITIES) + ".")
    lines.append("")
    return lines


# ======================= American Stories side ==========================


def _load_as_pages(wanted: set[tuple[str, str]]):
    """Return ({(lccn, date, page_str): page_record}, note).

    page_record = {"regions": [{text, legibility, y0, y1}], "page": str,
                   "full_text": str}. Only records for our papers on
    dates we actually have quarantined chunks are kept (memory bound).
    Records with page_number == "na" are kept under page key "na" and
    resolved by fragment disambiguation at join time.
    """
    try:
        from datasets import load_dataset

        ds = load_dataset(
            "dell-research-harvard/AmericanStories",
            "subset_years_content_regions",
            year_list=["1845"],
            trust_remote_code=True,
        )
        split = ds["1845"] if "1845" in ds else ds[list(ds.keys())[0]]
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        return None, f"**Failed to load AS 1845:** `{type(e).__name__}: {e}`\n```\n{tb}\n```"

    wanted_dates = {(l, d) for l, d in wanted}
    pages: dict[tuple[str, str, str], dict] = {}
    kept = 0
    for rec in split:
        raw = rec.get("raw_data_string")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        lccn = _dig(data, "lccn", "lccn") or ""
        if lccn not in TARGET_LCCNS:
            continue
        date = (_dig(data, "edition", "date")
                or _dig(data, "scan", "date") or "")
        if (lccn, date) not in wanted_dates:
            continue
        page = str(data.get("page_number", "na")).strip() or "na"
        regions = []
        for bb in data.get("bboxes", []) or []:
            txt = bb.get("raw_text") or ""
            if not txt:
                continue
            box = bb.get("bbox") or {}
            regions.append({
                "text": txt,
                "legibility": bb.get("legibility"),
                "y0": box.get("y0"),
                "y1": box.get("y1"),
            })
        full_text = "\n".join(r["text"] for r in regions)
        key = (lccn, date, page)
        # Some (lccn, date, "na") keys can collide; keep the larger scan.
        if key not in pages or len(full_text) > len(pages[key]["full_text"]):
            pages[key] = {"regions": regions, "page": page,
                          "full_text": full_text}
            kept += 1

    note = (f"Loaded AS 1845 content-regions; kept {len(pages):,} page "
            f"records matching our quarantined (lccn, date) universe "
            f"({len(wanted_dates):,} paper-dates wanted).")
    return pages, note


def _dig(obj, *keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# ======================= Step 4: JOIN ===================================


def _join(chunks, fragments, as_pages):
    """Attach clean text to each chunk. Returns (eval_rows, stats)."""
    stats = Counter()
    eval_rows: list[dict] = []

    # Index AS pages by (lccn, date) for fallback disambiguation.
    by_paper_date: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for (lccn, date, _page), rec in as_pages.items():
        by_paper_date[(lccn, date)].append(rec)

    for cid, ch in chunks.items():
        frags = [f for f, kind in fragments.get(cid, [])
                 if kind == "capital"][:ALIGN_MAX_FRAGMENTS]
        if len(frags) < ALIGN_MIN_HITS:
            # pad with dict fragments if capitals are scarce
            frags += [f for f, kind in fragments.get(cid, [])
                      if kind == "dict"][:ALIGN_MAX_FRAGMENTS - len(frags)]

        key = (ch["lccn"], ch["date"], str(ch["sequence"]))
        page_rec = as_pages.get(key)
        join_method = "exact_page"

        if page_rec is None:
            # Fallback: any scan for that paper-date; pick the one whose
            # regions contain the most of our fragments.
            candidates = by_paper_date.get((ch["lccn"], ch["date"]), [])
            if not candidates:
                stats["no_page_match"] += 1
                continue
            page_rec = max(
                candidates,
                key=lambda rec: _hit_count(frags, rec["full_text"]),
            )
            join_method = "date_fragment_disambiguated"

        # Region alignment within the page.
        region_text, align_score, aligned = _align_region(frags, page_rec)
        stats[join_method] += 1
        stats["region_aligned" if aligned else "page_level"] += 1

        clean = region_text if aligned else page_rec["full_text"]
        ch_out = dict(ch)
        ch_out.pop("embedding", None)  # not serializable; kept in memory map
        ch_out.update({
            "join_method": join_method,
            "align_score": round(align_score, 3),
            "label_scope": "region" if aligned else "page",
            "clean_text": clean[:PAGE_TEXT_TRIM],
        })
        eval_rows.append(ch_out)

    return eval_rows, stats


def _hit_count(frags: list[str], text: str) -> int:
    low = text.lower()
    return sum(1 for f in frags if f.lower() in low)


def _align_region(frags: list[str], page_rec: dict):
    """Best region by fragment hits. Returns (text, score, aligned?)."""
    if not frags:
        return "", 0.0, False
    best_text, best_hits = "", 0
    for region in page_rec["regions"]:
        hits = _hit_count(frags, region["text"])
        if hits > best_hits:
            best_hits, best_text = hits, region["text"]
    frac = best_hits / max(1, len(frags))
    aligned = best_hits >= ALIGN_MIN_HITS and frac >= ALIGN_MIN_FRACTION
    return best_text, frac, aligned


def _join_report(stats: Counter, total_chunks: int) -> list[str]:
    lines = ["## Join (step 4)", ""]
    joined = stats["exact_page"] + stats["date_fragment_disambiguated"]
    lines.append(f"- Quarantined chunks: {total_chunks:,}")
    lines.append(f"- Joined to clean AS text: **{joined:,}** "
                 f"({stats['exact_page']:,} exact (lccn,date,page); "
                 f"{stats['date_fragment_disambiguated']:,} date-level, "
                 "fragment-disambiguated)")
    lines.append(f"- No AS scan found for the page: {stats['no_page_match']:,}")
    lines.append(f"- Label scope: {stats['region_aligned']:,} region-aligned, "
                 f"{stats['page_level']:,} page-level")
    lines.append("")
    return lines


# ======================= Step 5: LABEL ==================================


_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _term_re(term: str) -> re.Pattern:
    pat = _WORD_RE_CACHE.get(term)
    if pat is None:
        pat = re.compile(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])")
        _WORD_RE_CACHE[term] = pat
    return pat


def _label(eval_rows: list[dict]) -> Counter:
    stats = Counter()
    ambiguous: list[dict] = []

    for row in eval_rows:
        low = row["clean_text"].lower()
        strong = [t for t in STRONG_TERMS if _term_re(t).search(low)]
        ents = [t for t in THEME_ENTITIES if _term_re(t).search(low)]
        weak = [t for t in WEAK_TERMS if _term_re(t).search(low)]

        if strong or (ents and weak):
            row["label"] = 1
            row["label_method"] = "strong_lexicon"
            row["label_terms"] = strong + ents + weak
            stats["strong_lexicon"] += 1
        elif weak or ents:
            row["label"] = 0  # provisional; Haiku may flip
            row["label_method"] = "ambiguous"
            row["label_terms"] = weak + ents
            ambiguous.append(row)
        else:
            row["label"] = 0
            row["label_method"] = "zero_hit"
            row["label_terms"] = []
            stats["zero_hit"] += 1

    stats["ambiguous_total"] = len(ambiguous)
    _haiku_resolve(ambiguous, stats)
    return stats


def _haiku_resolve(ambiguous: list[dict], stats: Counter) -> None:
    """Resolve up to HAIKU_CAP ambiguous rows; the rest stay negative
    with method 'ambiguous_unresolved'."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not ambiguous:
        for row in ambiguous:
            row["label_method"] = "ambiguous_unresolved"
            stats["ambiguous_unresolved"] += 1
        if ambiguous and not api_key:
            stats["haiku_skipped_no_key"] = 1
        return

    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
    system = (
        "You label 1845 newspaper text. Answer with exactly YES or NO: "
        "is this text substantively about political violence — riots, "
        "mobs, the Anti-Rent conflict, armed resistance to authority, "
        "politically motivated killings — as news or commentary? "
        "Crime reports without political dimension are NO. "
        "Advertisements and shipping/market lists are NO."
    )
    # Deterministic order: worst-first doesn't matter; cap is what matters.
    for row in ambiguous[:HAIKU_CAP]:
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL, max_tokens=4, temperature=0,
                system=system,
                messages=[{"role": "user",
                           "content": row["clean_text"][:2500]}],
            )
            text = "".join(b.text for b in msg.content
                           if hasattr(b, "text")).strip().upper()
            row["label"] = 1 if text.startswith("Y") else 0
            row["label_method"] = "haiku"
            stats["haiku"] += 1
        except Exception:  # noqa: BLE001 — cap + report, never loop
            row["label_method"] = "ambiguous_unresolved"
            stats["haiku_errors"] += 1
    for row in ambiguous[HAIKU_CAP:]:
        row["label_method"] = "ambiguous_unresolved"
        stats["ambiguous_unresolved"] += 1


def _label_report(stats: Counter, eval_rows: list[dict]) -> list[str]:
    pos = sum(r["label"] for r in eval_rows)
    lines = ["## Labels (step 5)", ""]
    lines.append(f"- Eval set: **{len(eval_rows):,}** chunks; "
                 f"**{pos:,} positive** ({pos / max(1, len(eval_rows)):.1%} "
                 "base rate)")
    for k in ("strong_lexicon", "zero_hit", "ambiguous_total", "haiku",
              "haiku_errors", "ambiguous_unresolved", "haiku_skipped_no_key"):
        if stats.get(k):
            lines.append(f"- {k}: {stats[k]:,}")
    lines.append("")
    return lines


# ======================= Step 6: SPLIT ==================================


def _split(eval_rows: list[dict]):
    rows = sorted(eval_rows, key=lambda r: r["chunk_id"])
    rng = random.Random(SEED)
    rng.shuffle(rows)
    n_tune = int(len(rows) * TUNE_FRACTION)
    return rows[:n_tune], rows[n_tune:]


# ======================= Step 7: BASELINE ===============================


def _score_theme_baseline(conn, eval_rows, theme_clusters,
                          commercial_clusters, clusters) -> None:
    """Theme-conditioned composite with the CURRENT weights, computed
    from the GARBLED side only. Mutates rows in place."""
    # Theme centroid matrix for proximity.
    theme_labels = [l for l in sorted(theme_clusters)
                    if clusters[l]["centroid"] is not None]
    if theme_labels:
        mat = np.stack([clusters[l]["centroid"] for l in theme_labels])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat_n = mat / norms
    else:
        mat_n = None

    # Theme entity surfaces: gazetteer entries tied to theme clusters,
    # plus lexicon terms with damage variants.
    theme_entities = _theme_entity_surfaces(conn, theme_clusters)
    lex_variants: set[str] = set()
    for term in (*STRONG_TERMS, *THEME_ENTITIES):
        for tok in term.split():
            if len(tok) >= 4:
                lex_variants |= {v.lower() for v in damage_variants(tok)}

    # Per-chunk theme entity-match via the stored A3 matches.
    theme_match = _theme_entity_match(conn, theme_entities)

    # Embeddings were dropped from rows for serialization; re-fetch map.
    emb = _embeddings_for(conn, [r["chunk_id"] for r in eval_rows])

    for row in eval_rows:
        # S_lex — lexicon vs surviving fragments + raw garbled text.
        s_lex = _lex_score(conn, row, lex_variants)
        # S_ent — stored fuzzy matches restricted to theme entities.
        s_ent = theme_match.get(row["chunk_id"], 0.0)
        entity_term = max(s_lex, s_ent)

        # Grid, theme-conditioned.
        if row["grid_guess"] is None:
            s_grid = 0.0
        elif row["grid_guess"] in theme_clusters:
            s_grid = abs(row["grid_conf"])
        elif row["grid_guess"] in commercial_clusters:
            s_grid = -abs(row["grid_conf"])
        else:
            s_grid = 0.0

        # Footprint, theme-conditioned.
        s_fp = (row["footprint_score"]
                if row["footprint_label"] in theme_clusters else 0.0)

        # Proximity to nearest THEME centroid, quality-weighted.
        s_prox = 0.0
        v = emb.get(row["chunk_id"])
        if mat_n is not None and v is not None:
            vn = np.linalg.norm(v)
            if vn > 0:
                sim = float(np.max(mat_n @ (v / vn)))
                qf = max(0.0, row["quality"] - PROXIMITY_QUALITY_FLOOR) \
                    / (1.0 - PROXIMITY_QUALITY_FLOOR)
                s_prox = max(0.0, sim) * qf

        # Commerciality of the chunk's own cluster.
        c0 = row["cluster_t0"]
        if c0 in commercial_clusters:
            s_comm = -1.0
        elif c0 in theme_clusters:
            s_comm = 1.0
        elif c0 >= 0 and clusters.get(c0, {}).get("label_text") \
                and not is_refusal(clusters[c0]["label_text"]):
            s_comm = 0.5
        else:
            s_comm = 0.0

        gap_bonus = (GAP_BONUS if row["gap_label"] in theme_clusters
                     else 1.0)

        prior = (W_ENTITY * entity_term + W_GRID * s_grid
                 + W_FOOTPRINT * s_fp + W_PROXIMITY * s_prox
                 + W_COMMERCIALITY * s_comm)
        score = max(0.0, prior + W_RECOVER * row["quality"]) * gap_bonus

        row["components"] = {
            "lex": round(s_lex, 3), "entity": round(s_ent, 3),
            "grid": round(s_grid, 3), "footprint": round(s_fp, 3),
            "proximity": round(s_prox, 3), "commerciality": round(s_comm, 2),
            "gap_bonus": gap_bonus, "quality": round(row["quality"], 3),
        }
        row["theme_score"] = round(score, 4)


def _theme_entity_surfaces(conn, theme_clusters) -> set[str]:
    if not theme_clusters:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT surface, cluster_t0 FROM entity_gazetteer"
        )
        out = {
            surface.lower()
            for surface, labs in cur.fetchall()
            if labs and set(labs) & theme_clusters
        }
    conn.commit()
    return out


def _theme_entity_match(conn, theme_entities: set[str]) -> dict[str, float]:
    if not theme_entities:
        return {}
    out: dict[str, float] = {}
    with conn.cursor(name="g2_matches") as cur:
        cur.itersize = 5000
        cur.execute(
            "SELECT chunk_id, entity_surface, similarity "
            "FROM quarantine_entity_matches"
        )
        for chunk_id, entity, sim in cur:
            if entity.lower() in theme_entities:
                cid = str(chunk_id)
                out[cid] = max(out.get(cid, 0.0), float(sim))
    conn.commit()
    return out


_FRAG_CACHE: dict[str, list[str]] = {}


def _lex_score(conn, row, lex_variants: set[str]) -> float:
    """Fragments (and raw garbled text) vs theme lexicon + variants."""
    cid = row["chunk_id"]
    frags = _FRAG_CACHE.get(cid)
    if frags is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fragment FROM quarantine_fragments WHERE chunk_id = %s",
                (UUID(cid),),
            )
            frags = [f[0].lower() for f in cur.fetchall()]
        conn.commit()
        _FRAG_CACHE[cid] = frags

    best = 0.0
    for f in frags:
        if f in lex_variants:
            return 1.0
        if len(f) >= 5:
            for term in lex_variants:
                if abs(len(term) - len(f)) <= 2:
                    r = difflib.SequenceMatcher(None, f, term).ratio()
                    if r >= FUZZY_LEX_MIN and r > best:
                        best = r
    # Raw substring pass on the garbled content for multiword phrases.
    low = row["content"].lower()
    for term in STRONG_TERMS:
        if term in low:
            return 1.0
    return best


def _embeddings_for(conn, chunk_ids: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    batch = 500
    with conn.cursor() as cur:
        for i in range(0, len(chunk_ids), batch):
            ids = [UUID(c) for c in chunk_ids[i:i + batch]]
            cur.execute(
                "SELECT id, embedding FROM chunks "
                "WHERE id = ANY(%s) AND embedding IS NOT NULL",
                (ids,),
            )
            for cid, vec in cur.fetchall():
                out[str(cid)] = np.asarray(vec, dtype=np.float32)
    conn.commit()
    return out


# ======================= Metrics + report ===============================


def _precision_at(rows: list[dict], k: int) -> float:
    ranked = sorted(rows, key=lambda r: -r["theme_score"])[:k]
    return sum(r["label"] for r in ranked) / max(1, len(ranked))


def _recall_at(rows: list[dict], k: int) -> float:
    pos = sum(r["label"] for r in rows)
    if pos == 0:
        return 0.0
    ranked = sorted(rows, key=lambda r: -r["theme_score"])[:k]
    return sum(r["label"] for r in ranked) / pos


def _baseline_report(tune: list[dict], holdout: list[dict]) -> list[str]:
    lines = ["## Baseline (step 7) — current heuristic, theme-scoped", ""]
    lines.append("| set | n | positives | P@15 | P@20 | R@15 | R@20 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for name, rows in (("TUNE", tune), ("HOLDOUT", holdout)):
        pos = sum(r["label"] for r in rows)
        p15, p20 = _precision_at(rows, 15), _precision_at(rows, 20)
        r15, r20 = _recall_at(rows, 15), _recall_at(rows, 20)
        lines.append(f"| {name} | {len(rows):,} | {pos:,} | "
                     f"{p15:.2f} | {p20:.2f} | {r15:.2f} | {r20:.2f} |")
    lines.append("")

    lines.append("### Top 15 flagged (TUNE) — component breakdown")
    lines.append("")
    for r in sorted(tune, key=lambda r: -r["theme_score"])[:15]:
        mark = "✓" if r["label"] else "✗"
        lines.append(
            f"- {mark} score={r['theme_score']:.3f} "
            f"{r['lccn']} {r['date']} p.{r['sequence']} "
            f"[{r['label_method']}] {json.dumps(r['components'])}"
        )
    lines.append("")

    lines.extend(_error_lists(tune))
    return lines


def _error_lists(rows: list[dict]) -> list[str]:
    lines: list[str] = []
    ranked = sorted(rows, key=lambda r: -r["theme_score"])
    top20 = ranked[:20]
    fps = [r for r in top20 if not r["label"]][:10]
    positives = [r for r in rows if r["label"]]
    fns = sorted(positives, key=lambda r: r["theme_score"])[:10]

    lines.append("### Worst false positives (flagged in top 20, off-topic)")
    lines.append("")
    for r in fps:
        snip = r["clean_text"][:300].replace("\n", " ")
        lines.append(f"- score={r['theme_score']:.3f} {r['lccn']} {r['date']} "
                     f"p.{r['sequence']} — {json.dumps(r['components'])}")
        lines.append(f"  > clean: _{snip}_")
    if not fps:
        lines.append("_(none — top 20 all on-topic)_")
    lines.append("")

    lines.append("### Worst false negatives (on-topic, ranked lowest)")
    lines.append("")
    for r in fns:
        snip = r["clean_text"][:300].replace("\n", " ")
        lines.append(f"- score={r['theme_score']:.3f} {r['lccn']} {r['date']} "
                     f"p.{r['sequence']} [{r['label_method']}] — "
                     f"{json.dumps(r['components'])}")
        lines.append(f"  > clean: _{snip}_")
    if not fns:
        lines.append("_(no positives in the set)_")
    lines.append("")
    return lines


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


def _write_report(lines: list[str]) -> None:
    report = "\n".join(lines)
    REPORT_PATH.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
