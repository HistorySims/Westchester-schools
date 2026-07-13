"""Quarantine Recovery System — Phase A batch.

Pure free signals: gazetteer from active text, fragment extraction from
quarantined text, fuzzy entity matching with deterministic OCR damage
variants, layout grid model, footprint + gap detection, quality-weighted
embedding proximity, and the composite recovery_value. Every component
is stored individually on chunk_recovery so question-scoped scoring
can reweight at query time without re-running this batch.

Writes diagnostic files for Checkpoint 1:
  scripts/recovery_grid_report.md
  scripts/recovery_fuzzy_samples.md
  scripts/recovery_top_candidates.md

Idempotent — truncates and rewrites every table this batch owns.

Usage:
    uv run scripts/recovery_score.py
"""

from __future__ import annotations

import random
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from herald import settings


# --------------------- Tunable constants -------------------------------

# A1 — gazetteer
# Single tokens are noisy at low frequencies (Checkpoint-1: "Bank",
# "RIVER", "FROM" surfaced because they hit ≥3 chunks). Tighten:
#   - exclude any single token whose lowercase is in the wordlist
#     (entity ≈ "looks like a proper noun but isn't English vocab")
#   - require a higher chunk-frequency floor for singles
#   - require a minimum length and clean Capitalized/ALLCAPS shape
#     so OCR artifacts like "THis", "Hank" don't slip in
# Multi-word spans are unambiguous (e.g. "Smith Boughton") and stay
# at the looser thresholds — these are rare-by-construction.
GAZETTEER_MIN_CHUNK_FREQ_SINGLE = 5
GAZETTEER_MIN_CHUNK_FREQ_MULTI = 3
GAZETTEER_MIN_SINGLE_LEN = 5
GAZETTEER_MAX_TOKEN_LEN = 32
GAZETTEER_MIN_CAPITAL_LEN = 3
GAZETTEER_MAX_ENTRIES = 50_000     # safety cap

# A2 — fragment extraction
FRAGMENT_CAPITAL_MIN_LEN = 4
FRAGMENT_MAX_PER_CHUNK = 80        # truncate runaway garbage

# A3 — fuzzy matching
ENTITY_MATCH_MIN_SIMILARITY = 0.55
ENTITY_MATCH_TOP_K_PER_CHUNK = 5
DAMAGE_VARIANT_MAX_PER_ENTITY = 12  # bound combinatorial blow-up

# A4 — layout grid
GRID_POSITION_BUCKETS = 5
GRID_REGULAR_THRESHOLD = 0.60
GRID_MIN_SAMPLES_FOR_REGULAR = 4   # below this, slot is too sparse

# A5 — gap
GAP_BONUS = 2.0
GAP_DAY_WINDOW = 1                  # D-1 / D+1 (i.e. one-day gap)

# A6 — proximity
PROXIMITY_QUALITY_FLOOR = 0.10
PROXIMITY_NEAREST_K = 3             # report nearest, score on top-1

# A7 — composite
# relevance_prior weighted blend:
W_ENTITY = 0.35
W_GRID = 0.20
W_FOOTPRINT = 0.20
W_PROXIMITY = 0.25
# Recoverability now an additive term, not a multiplier. Checkpoint-1
# showed the multiplicative form crushed every quarantined chunk —
# quality ≈ 0.01–0.04 on this population zeroed the composite. Keep
# quality as a tie-breaker the user can read in the breakdown.
W_RECOVER = 0.10
# Cluster commerciality bias — directly added to relevance_prior
# regardless of slot regularity. The grid signal only fires for the
# few slots dense enough to be "regular" (7 out of 48 in this corpus),
# which misses the majority of commercial-cluster chunks. This term
# applies the commercial/editorial judgment cluster-wide:
#   - commercial cluster → -1.0 signal
#   - cluster with a real (non-refusal) label → +0.5 signal
#   - unlabeled / too-small-to-label cluster → 0 (neutral)
# Effective magnitude per chunk = W_COMMERCIALITY × signal.
W_COMMERCIALITY = 0.20
# Grid sign: clusters labeled with these substrings are treated as
# non-substantive (ads, prices, schedules, legal notices, etc.).
# Substring matched against ' ' + label.lower() + ' ' so leading-/
# trailing-bounded variants like ' ad ' match a standalone "ad" but
# not "masthead" or "broadcast". Each entry must include its own
# whitespace boundaries when whole-word semantics are required;
# unbounded substrings (e.g. "advertis") still catch inflections.
COMMERCIAL_LABEL_KEYWORDS = (
    "advertisement", "advertis",     # advertisements, advertising
    " ad ", " ads ",                  # standalone — boundary-bounded
    " price ", " prices ", "commodity", "market report",
    "schedule", " rates ",
    "legal notice", " notice ", "mortgage", " attachment ",
    "foreclosure", "debtor", "summons",
    " retail ", " sale ", " sales ",
    "testimonial", "patent medicine", " remedy", "remedies",
    "insurance company",
    " hotel",
)

# Diagnostic file knobs
GRID_REPORT_TOP_N = 20
FUZZY_SAMPLES_N = 30
TOP_CANDIDATES_N = 20

# OCR damage substitutions used for variant generation. Each entry is
# (pattern, replacement). Single-replacement variants only — keep the
# fan-out manageable.
DAMAGE_SUBSTITUTIONS: list[tuple[str, str]] = [
    ("s", "f"), ("f", "s"),    # long-s ↔ f
    ("e", "c"), ("c", "e"),
    ("rn", "m"), ("m", "rn"),
    ("li", "h"), ("h", "li"),
    ("o", "0"), ("0", "o"),
    ("l", "1"), ("1", "l"),
    ("l", "I"), ("I", "l"),
    ("h", "b"), ("b", "h"),
]


SCRIPT_DIR = Path(__file__).parent
WORDLIST_PATH = SCRIPT_DIR.parent / "src" / "herald" / "wordlist.txt"


# --------------------- Main orchestration ------------------------------


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)

    try:
        print("Loading wordlist...")
        wordlist = _load_wordlist()
        print(f"  {len(wordlist):,} dictionary tokens")

        print("Loading active cluster run...")
        run_id = _active_run(conn)

        print("Loading per-page chunk counts (shared bucket denominator)...")
        page_counts = _load_page_counts(conn)

        print("Loading active chunks (text, position, page, paper, cluster)...")
        active_chunks = _load_active_chunks(conn, run_id, page_counts)
        print(f"  {len(active_chunks):,} active chunks")

        print("Loading quarantined chunks...")
        quarantined = _load_quarantined_chunks(conn, page_counts)
        print(f"  {len(quarantined):,} quarantined chunks")
        if not quarantined:
            print("Nothing quarantined — exiting.")
            return

        print("Loading cluster labels (for commercial/editorial classification)...")
        labels_by_t0 = _label_text_by_label(conn, run_id)
        all_labels_by_t0 = _label_text_by_label(conn, run_id, include_refusals=True)
        commercial_labels = classify_commercial(all_labels_by_t0)
        print(f"  {len(labels_by_t0):,} labeled clusters; "
              f"{len(commercial_labels):,} flagged commercial")

        print("A1 — building gazetteer from active text...")
        gazetteer = build_gazetteer(active_chunks, wordlist)
        n_multi = sum(1 for v in gazetteer.values() if v["is_multiword"])
        print(f"  {len(gazetteer):,} entries ({n_multi:,} multiword, "
              f"{len(gazetteer) - n_multi:,} single)")

        print("A2 — extracting fragments from quarantined chunks...")
        fragments = extract_fragments(quarantined, wordlist)
        n_frag = sum(len(v) for v in fragments.values())
        print(f"  {n_frag:,} fragments across {len(fragments):,} chunks")

        print("Writing entity_gazetteer + quarantine_fragments...")
        _write_gazetteer(conn, gazetteer)
        _write_fragments(conn, fragments)

        print("A3 — fuzzy matching fragments against gazetteer + damage variants...")
        matches = fuzzy_match(conn, gazetteer)
        n_match = sum(len(v) for v in matches.values())
        print(f"  {n_match:,} matches across {len(matches):,} chunks")
        _write_matches(conn, matches)

        print("A4 — building layout grid from active chunks...")
        slots, grid_violations = build_layout_grid(active_chunks, commercial_labels)
        regular = [s for s in slots.values() if s["top_label_share"] >= GRID_REGULAR_THRESHOLD
                   and s["sample_size"] >= GRID_MIN_SAMPLES_FOR_REGULAR]
        n_commercial = sum(1 for s in regular if s.get("is_commercial"))
        print(f"  {len(slots):,} slots, {len(regular):,} regular "
              f"({n_commercial:,} commercial → negative sign)")
        _write_slots(conn, slots)

        print("A5 — computing cluster footprints and gap candidates...")
        footprints = build_footprints(active_chunks)
        gaps = detect_gaps(active_chunks, footprints, quarantined)
        print(f"  {len(footprints):,} cluster footprints, {len(gaps):,} gap candidates")

        print("A6 — quality-weighted proximity to active centroids...")
        centroids = _load_active_centroids(conn, run_id)
        print(f"  {len(centroids):,} cluster centroids")
        proximity = compute_proximity(quarantined, centroids)

        print("A7 — assembling composite scores...")
        chunk_to_cluster_t0 = _load_quarantined_cluster_t0(conn, run_id)
        labeled_cluster_labels = set(all_labels_by_t0.keys())
        per_chunk = assemble_recovery(
            quarantined=quarantined,
            matches=matches,
            slots=slots,
            footprints=footprints,
            gaps=gaps,
            proximity=proximity,
            commercial_labels=commercial_labels,
            labeled_cluster_labels=labeled_cluster_labels,
            chunk_to_cluster_t0=chunk_to_cluster_t0,
        )
        print(f"  {len(per_chunk):,} chunk_recovery rows")
        _write_chunk_recovery(conn, per_chunk)

        print("Writing diagnostic files...")
        write_grid_report(
            slots, grid_violations, active_chunks, labels_by_t0, commercial_labels,
        )
        write_fuzzy_samples(conn, matches)
        write_top_candidates(per_chunk, quarantined, labels_by_t0)
        print("Done.")

    finally:
        conn.close()


# --------------------- Loaders -----------------------------------------


def _active_run(conn: psycopg.Connection) -> UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM active_cluster_run WHERE singleton = true")
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No active cluster run")
        rid = row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
    conn.commit()
    return rid


def _load_wordlist() -> set[str]:
    text = WORDLIST_PATH.read_text()
    return {w.strip().lower() for w in text.splitlines() if w.strip()}


class ActiveChunk:
    __slots__ = ("id", "content", "page_id", "paper_id", "lccn", "page_sequence",
                 "chunk_index", "page_total", "date_issued", "cluster_t0",
                 "position_bucket")
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class QuarantinedChunk:
    __slots__ = ("id", "content", "page_id", "paper_id", "lccn", "page_sequence",
                 "chunk_index", "page_total", "date_issued", "edition",
                 "position_bucket", "embedding", "quality")
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _load_page_counts(conn: psycopg.Connection) -> dict[UUID, int]:
    """Chunks per page over ALL current chunks (active + quarantined).

    Position buckets must mean physical position on the page, so the
    denominator is the full page regardless of status — otherwise the
    slot keys learned from active chunks wouldn't line up with the
    buckets assigned to quarantined chunks on the same page.
    """
    page_counts: dict[UUID, int] = {}
    with conn.cursor(name="all_page_counts_shared") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT page_id, COUNT(*)::int
              FROM chunks
             WHERE is_current = true
             GROUP BY page_id
            """,
        )
        for pid, n in cur:
            page_counts[pid if isinstance(pid, UUID) else UUID(str(pid))] = int(n)
    conn.commit()
    return page_counts


def _load_active_chunks(
    conn: psycopg.Connection, run_id: UUID, page_counts: dict[UUID, int],
) -> list[ActiveChunk]:
    """Active chunks with page position + cluster_t0 assignment."""
    out: list[ActiveChunk] = []
    with conn.cursor(name="active_chunks_load") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT chunks.id, chunks.content, chunks.page_id, chunks.chunk_index,
                   papers.id, papers.lccn, pages.sequence,
                   issues.date_issued, cp.cluster_t0
              FROM chunks
              JOIN chunk_projections cp ON cp.chunk_id = chunks.id
              JOIN pages  ON pages.id = chunks.page_id
              JOIN issues ON issues.id = pages.issue_id
              JOIN papers ON papers.id = issues.paper_id
             WHERE chunks.status = 'active'
               AND chunks.is_current = true
               AND cp.run_id = %s
               AND cp.content_type = 0
               AND cp.cluster_t0 >= 0
            """,
            (run_id,),
        )
        for r in cur:
            page_id = r[2] if isinstance(r[2], UUID) else UUID(str(r[2]))
            total = page_counts.get(page_id, 1) or 1
            chunk_idx = int(r[3])
            bucket = _position_bucket(chunk_idx, total)
            out.append(ActiveChunk(
                id=r[0] if isinstance(r[0], UUID) else UUID(str(r[0])),
                content=r[1],
                page_id=page_id,
                paper_id=r[4] if isinstance(r[4], UUID) else UUID(str(r[4])),
                lccn=r[5],
                page_sequence=int(r[6]),
                chunk_index=chunk_idx,
                page_total=total,
                date_issued=r[7],
                cluster_t0=int(r[8]),
                position_bucket=bucket,
            ))
    conn.commit()
    return out


def _load_quarantined_chunks(
    conn: psycopg.Connection, page_counts: dict[UUID, int],
) -> list[QuarantinedChunk]:
    """All quarantined chunks with embedding + page/paper context."""
    out: list[QuarantinedChunk] = []
    with conn.cursor(name="quarantined_load") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT chunks.id, chunks.content, chunks.page_id, chunks.chunk_index,
                   papers.id, papers.lccn, pages.sequence,
                   issues.date_issued, issues.edition,
                   chunks.embedding, chunks.quality_score
              FROM chunks
              JOIN pages  ON pages.id = chunks.page_id
              JOIN issues ON issues.id = pages.issue_id
              JOIN papers ON papers.id = issues.paper_id
             WHERE chunks.status = 'quarantined'
               AND chunks.is_current = true
            """
        )
        for r in cur:
            page_id = r[2] if isinstance(r[2], UUID) else UUID(str(r[2]))
            total = page_counts.get(page_id, 1) or 1
            chunk_idx = int(r[3])
            emb = np.asarray(r[9], dtype=np.float32) if r[9] is not None else None
            out.append(QuarantinedChunk(
                id=r[0] if isinstance(r[0], UUID) else UUID(str(r[0])),
                content=r[1] or "",
                page_id=page_id,
                paper_id=r[4] if isinstance(r[4], UUID) else UUID(str(r[4])),
                lccn=r[5],
                page_sequence=int(r[6]),
                chunk_index=chunk_idx,
                page_total=total,
                date_issued=r[7],
                edition=int(r[8]),
                position_bucket=_position_bucket(chunk_idx, total),
                embedding=emb,
                quality=float(r[10]) if r[10] is not None else 0.0,
            ))
    conn.commit()
    return out


def _load_quarantined_cluster_t0(
    conn: psycopg.Connection, run_id: UUID,
) -> dict[UUID, int]:
    """Map quarantined chunk_id → its tier-0 cluster label.

    Quarantined chunks aren't returned by _load_active_chunks (that
    filters status='active'), but their cluster assignments still
    live on chunk_projections. The commerciality bias and gap-bonus
    gate both need cluster_t0 per chunk, regardless of status.
    """
    out: dict[UUID, int] = {}
    with conn.cursor(name="quarantined_cluster_t0") as cur:
        cur.itersize = 5000
        cur.execute(
            """
            SELECT cp.chunk_id, cp.cluster_t0
              FROM chunk_projections cp
              JOIN chunks ON chunks.id = cp.chunk_id
             WHERE cp.run_id = %s
               AND chunks.status = 'quarantined'
               AND chunks.is_current = true
            """,
            (run_id,),
        )
        for chunk_id, t0 in cur:
            cid = chunk_id if isinstance(chunk_id, UUID) else UUID(str(chunk_id))
            out[cid] = int(t0)
    conn.commit()
    return out


def _load_active_centroids(
    conn: psycopg.Connection, run_id: UUID,
) -> dict[int, np.ndarray]:
    """Fine-cluster active_centroid by label. Falls back to centroid
    when active_centroid hasn't been populated (pre-recompute)."""
    out: dict[int, np.ndarray] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, active_centroid, centroid
              FROM clusters
             WHERE run_id = %s AND tier = 0
            """,
            (run_id,),
        )
        for label, active_c, fallback_c in cur:
            vec = active_c if active_c is not None else fallback_c
            if vec is None:
                continue
            out[int(label)] = np.asarray(vec, dtype=np.float32)
    conn.commit()
    return out


def _position_bucket(chunk_index: int, page_total: int) -> int:
    if page_total <= 0:
        return 0
    frac = chunk_index / page_total
    b = int(frac * GRID_POSITION_BUCKETS)
    return max(0, min(GRID_POSITION_BUCKETS - 1, b))


# --------------------- A1 ----------------------------------------------


# Single-token regex: a Capitalized word, allowing apostrophe/hyphen.
TOKEN_RE = re.compile(r"\b[A-Z][a-zA-ZÀ-ſ'\-]{2,}\b")
# Sentence-end punctuation followed by space + Capitalized. The token
# AFTER such punctuation is "sentence-initial" and should be skipped.
SENT_INITIAL_RE = re.compile(r"[.!?]\s+([A-Z][a-zA-ZÀ-ſ'\-]{2,})")
# Multi-word capitalized span: ≥2 consecutive Capitalized tokens
# (allows "Van", "the", "of" between Caps — common 19th-c. names).
MULTIWORD_RE = re.compile(
    r"\b[A-Z][a-zA-ZÀ-ſ'\-]{2,}"
    r"(?:\s+(?:[a-z]{2,4}\s+)?[A-Z][a-zA-ZÀ-ſ'\-]{2,}){1,3}\b"
)


_CAPITALIZED_RE = re.compile(r"[A-Z][a-z]+$")
_ALLCAPS_RE = re.compile(r"[A-Z]+$")


def _is_clean_single(surface: str) -> bool:
    """Reject OCR-artifact case patterns like 'THis', 'FRom', 'Hank'.
    Accept only proper-noun-shaped (`Capitalized`) or all-caps
    surfaces (headline emphasis form)."""
    return bool(_CAPITALIZED_RE.match(surface) or _ALLCAPS_RE.match(surface))


def build_gazetteer(
    chunks: list[ActiveChunk],
    wordlist: set[str],
) -> dict[str, dict]:
    """Recurring proper-noun candidates from active text.

    Single tokens: Capitalized non-sentence-initial; clean case shape;
      lowercase NOT in the English wordlist; length ≥ MIN_SINGLE_LEN;
      hits ≥ MIN_CHUNK_FREQ_SINGLE distinct active chunks.

    Multi-word spans: kept at the looser MIN_CHUNK_FREQ_MULTI — these
      are unambiguously entity-like by construction.

    Returns surface → {freq, cluster_t0 list, is_multiword}.
    """
    chunk_counts: dict[str, set[UUID]] = defaultdict(set)
    cluster_sets: dict[str, set[int]] = defaultdict(set)
    multiword_set: set[str] = set()

    for ch in chunks:
        content = ch.content
        if not content:
            continue
        # Sentence-initial positions: start of content, or after .!?\s+
        sent_inits = {0}
        for m in re.finditer(r"[.!?]\s+", content):
            sent_inits.add(m.end())

        # Single tokens
        for m in TOKEN_RE.finditer(content):
            tok = m.group(0)
            if len(tok) > GAZETTEER_MAX_TOKEN_LEN:
                continue
            if m.start() in sent_inits:
                continue
            chunk_counts[tok].add(ch.id)
            cluster_sets[tok].add(ch.cluster_t0)

        # Multi-word spans (almost never spurious)
        for m in MULTIWORD_RE.finditer(content):
            span = re.sub(r"\s+", " ", m.group(0).strip())
            if len(span) > GAZETTEER_MAX_TOKEN_LEN * 2:
                continue
            chunk_counts[span].add(ch.id)
            cluster_sets[span].add(ch.cluster_t0)
            multiword_set.add(span)

    gazetteer: dict[str, dict] = {}
    for surface, ids in chunk_counts.items():
        is_multi = surface in multiword_set
        if is_multi:
            if len(ids) < GAZETTEER_MIN_CHUNK_FREQ_MULTI:
                continue
        else:
            # Single-token quality gate: clean case, ≥ min length,
            # not English vocab, and frequent enough.
            if len(ids) < GAZETTEER_MIN_CHUNK_FREQ_SINGLE:
                continue
            if len(surface) < GAZETTEER_MIN_SINGLE_LEN:
                continue
            if not _is_clean_single(surface):
                continue
            if surface.lower() in wordlist:
                continue
        gazetteer[surface] = {
            "freq": len(ids),
            "cluster_t0": sorted(cluster_sets[surface]),
            "is_multiword": is_multi,
        }

    if len(gazetteer) > GAZETTEER_MAX_ENTRIES:
        top = sorted(gazetteer.items(), key=lambda kv: -kv[1]["freq"])[:GAZETTEER_MAX_ENTRIES]
        gazetteer = dict(top)
    return gazetteer


# --------------------- A2 ----------------------------------------------


def extract_fragments(
    quarantined: list[QuarantinedChunk],
    wordlist: set[str],
) -> dict[UUID, list[tuple[str, str, int]]]:
    """Per chunk: [(fragment, kind, position), ...] for the chunk's
    surviving legible tokens."""
    out: dict[UUID, list[tuple[str, str, int]]] = {}
    word_tok = re.compile(r"\b[A-Za-z][A-Za-z'\-]{1,}\b")
    for ch in quarantined:
        if not ch.content:
            continue
        rows: list[tuple[str, str, int]] = []
        seen: set[str] = set()
        for pos, m in enumerate(word_tok.finditer(ch.content)):
            tok = m.group(0)
            low = tok.lower()
            kind: str | None = None
            if low in wordlist:
                kind = "dict"
            elif tok[0].isupper() and len(tok) >= FRAGMENT_CAPITAL_MIN_LEN:
                kind = "capital"
            if kind is None:
                continue
            key = f"{kind}:{tok}:{pos}"
            if key in seen:
                continue
            seen.add(key)
            rows.append((tok, kind, pos))
            if len(rows) >= FRAGMENT_MAX_PER_CHUNK:
                break
        if rows:
            out[ch.id] = rows
    return out


# --------------------- A3 ----------------------------------------------


def damage_variants(entity: str) -> set[str]:
    """Single-substitution OCR damage variants of an entity. Each
    variant replaces ONE occurrence of one pattern; capped per-entity."""
    out: set[str] = {entity}
    low = entity.lower()
    for patt, repl in DAMAGE_SUBSTITUTIONS:
        # Find every position of patt in low (case-insensitive); for each,
        # build a variant where ONLY that occurrence is replaced.
        start = 0
        while True:
            idx = low.find(patt, start)
            if idx == -1:
                break
            variant = entity[:idx] + repl + entity[idx + len(patt):]
            out.add(variant)
            if len(out) > DAMAGE_VARIANT_MAX_PER_ENTITY:
                return out
            start = idx + 1
    return out


def fuzzy_match(
    conn: psycopg.Connection,
    gazetteer: dict[str, dict],
) -> dict[UUID, list[dict]]:
    """For each quarantined chunk, return top-K entity matches blending
    direct and damage-variant similarity using pg_trgm.

    Strategy: pre-compute the union of (entity_surface, candidate_form,
    via_variant) and ship it as a temp values table; let pg_trgm compute
    similarity against quarantine_fragments in one pass per chunk batch.
    """
    # Build candidate forms with provenance.
    candidates: list[tuple[str, str, str]] = []  # (variant_form, canonical_surface, via)
    for surface in gazetteer:
        variants = damage_variants(surface)
        for v in variants:
            via = "direct" if v == surface else "damage"
            candidates.append((v, surface, via))

    # Set per-statement similarity threshold for pg_trgm '%' operator.
    out: dict[UUID, list[dict]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(f"SET pg_trgm.similarity_threshold = {ENTITY_MATCH_MIN_SIMILARITY}")
        # Stage candidates into a TEMP TABLE for indexed similarity joins.
        cur.execute("DROP TABLE IF EXISTS _recovery_candidates")
        cur.execute(
            """
            CREATE TEMP TABLE _recovery_candidates (
              variant_form text,
              canonical    text,
              via          text
            ) ON COMMIT DROP
            """,
        )
        cur.executemany(
            "INSERT INTO _recovery_candidates (variant_form, canonical, via) VALUES (%s, %s, %s)",
            candidates,
        )
        cur.execute(
            "CREATE INDEX ON _recovery_candidates USING gin (variant_form gin_trgm_ops)"
        )
        cur.execute("ANALYZE _recovery_candidates")

        # One query: for every fragment, find candidates above
        # similarity threshold. Rank by similarity DESC per chunk,
        # capping to TOP_K via a window function in Python.
        cur.execute(
            """
            SELECT qf.chunk_id, c.canonical, qf.fragment,
                   similarity(c.variant_form, qf.fragment) AS sim,
                   c.via
              FROM quarantine_fragments qf
              JOIN _recovery_candidates c
                ON c.variant_form %% qf.fragment
             WHERE similarity(c.variant_form, qf.fragment) >= %s
            """,
            (ENTITY_MATCH_MIN_SIMILARITY,),
        )
        for chunk_id, canonical, fragment, sim, via in cur:
            cid = chunk_id if isinstance(chunk_id, UUID) else UUID(str(chunk_id))
            out[cid].append({
                "entity": canonical,
                "fragment": fragment,
                "similarity": float(sim),
                "via": via,
            })
    conn.commit()

    # Per-chunk: keep best similarity per (entity, fragment), then top-K
    # by similarity DESC.
    capped: dict[UUID, list[dict]] = {}
    for cid, rows in out.items():
        best: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["entity"], r["fragment"])
            prev = best.get(key)
            if prev is None or r["similarity"] > prev["similarity"]:
                best[key] = r
        sorted_rows = sorted(best.values(), key=lambda r: -r["similarity"])
        capped[cid] = sorted_rows[:ENTITY_MATCH_TOP_K_PER_CHUNK]
    return capped


# --------------------- A4 ----------------------------------------------


def build_layout_grid(
    chunks: list[ActiveChunk],
    commercial_labels: set[int],
) -> tuple[dict[tuple[UUID, int, int], dict], list[dict]]:
    """Per (paper_id, page_sequence, position_bucket) compute
    cluster_t0 distribution over active chunks. Slots that clear
    GRID_REGULAR_THRESHOLD with ≥ GRID_MIN_SAMPLES_FOR_REGULAR get a
    `signed_share`:
      - commercial top label → NEGATIVE share (a quarantined chunk
        landing here is almost certainly steamboat schedules or ads;
        deprioritize as a recovery target).
      - editorial top label  → POSITIVE share (boost).
      - sub-threshold        → 0.
    Returns (slots_dict, violation_days)."""
    by_slot: dict[tuple[UUID, int, int], list[int]] = defaultdict(list)
    by_slot_chunks: dict[tuple[UUID, int, int], list[ActiveChunk]] = defaultdict(list)
    for ch in chunks:
        key = (ch.paper_id, ch.page_sequence, ch.position_bucket)
        by_slot[key].append(ch.cluster_t0)
        by_slot_chunks[key].append(ch)

    slots: dict[tuple[UUID, int, int], dict] = {}
    for key, labels in by_slot.items():
        if not labels:
            continue
        counter = Counter(labels)
        top_label, top_count = counter.most_common(1)[0]
        share = top_count / len(labels)
        sample = len(labels)
        is_regular = (
            share >= GRID_REGULAR_THRESHOLD
            and sample >= GRID_MIN_SAMPLES_FOR_REGULAR
        )
        is_commercial = int(top_label) in commercial_labels
        if not is_regular:
            signed_share = 0.0
        elif is_commercial:
            signed_share = -share
        else:
            signed_share = +share
        slots[key] = {
            "paper_id": key[0],
            "page_sequence": key[1],
            "position_bucket": key[2],
            "top_label": int(top_label),
            "top_label_share": share,
            "signed_share": signed_share,
            "is_commercial": is_commercial,
            "is_regular": is_regular,
            "top_content_type": 0,  # we only loaded content_type=0
            "top_content_share": 1.0,
            "sample_size": sample,
        }

    # Violations: chunks in regular slots whose own label != top.
    violations: list[dict] = []
    for key, members in by_slot_chunks.items():
        slot = slots.get(key)
        if not slot:
            continue
        if (
            slot["top_label_share"] < GRID_REGULAR_THRESHOLD
            or slot["sample_size"] < GRID_MIN_SAMPLES_FOR_REGULAR
        ):
            continue
        for ch in members:
            if ch.cluster_t0 != slot["top_label"]:
                violations.append({
                    "date": ch.date_issued,
                    "lccn": ch.lccn,
                    "page_sequence": ch.page_sequence,
                    "bucket": ch.position_bucket,
                    "expected_label": slot["top_label"],
                    "actual_label": ch.cluster_t0,
                    "chunk_id": ch.id,
                })
    return slots, violations


# --------------------- A5 ----------------------------------------------


class ClusterFootprint:
    __slots__ = ("label", "lccns", "slot_keys", "date_min", "date_max", "active_dates")
    def __init__(self):
        self.label: int = -1
        self.lccns: set[str] = set()
        self.slot_keys: set[tuple[UUID, int, int]] = set()
        self.date_min: date | None = None
        self.date_max: date | None = None
        self.active_dates: set[date] = set()


def build_footprints(
    chunks: list[ActiveChunk],
) -> dict[int, ClusterFootprint]:
    by_label: dict[int, ClusterFootprint] = {}
    for ch in chunks:
        fp = by_label.get(ch.cluster_t0)
        if fp is None:
            fp = ClusterFootprint()
            fp.label = ch.cluster_t0
            by_label[ch.cluster_t0] = fp
        fp.lccns.add(ch.lccn)
        fp.slot_keys.add((ch.paper_id, ch.page_sequence, ch.position_bucket))
        fp.active_dates.add(ch.date_issued)
        if fp.date_min is None or ch.date_issued < fp.date_min:
            fp.date_min = ch.date_issued
        if fp.date_max is None or ch.date_issued > fp.date_max:
            fp.date_max = ch.date_issued
    return by_label


def detect_gaps(
    active: list[ActiveChunk],
    footprints: dict[int, ClusterFootprint],
    quarantined: list[QuarantinedChunk],
) -> dict[UUID, int]:
    """A quarantined chunk is a gap candidate for cluster C when:
       - C is active on (date - 1) and (date + 1)
       - C has no active chunk on date itself
       - The quarantined chunk's slot is in C's footprint slot_keys

    Returns chunk_id → cluster_t0 label."""
    # Index active chunks by (lccn, date) for fast lookup.
    out: dict[UUID, int] = {}
    for q in quarantined:
        slot = (q.paper_id, q.page_sequence, q.position_bucket)
        d = q.date_issued
        prev_d = d - timedelta(days=GAP_DAY_WINDOW)
        next_d = d + timedelta(days=GAP_DAY_WINDOW)
        # Try each cluster whose footprint includes this slot AND lccn,
        # and check the surrounding-day condition.
        best_label: int | None = None
        for label, fp in footprints.items():
            if q.lccn not in fp.lccns:
                continue
            if slot not in fp.slot_keys:
                continue
            if d in fp.active_dates:
                continue
            if prev_d in fp.active_dates and next_d in fp.active_dates:
                # Tie-break to highest-coverage cluster.
                if best_label is None or fp.date_max > footprints[best_label].date_max:
                    best_label = label
        if best_label is not None:
            out[q.id] = best_label
    return out


# --------------------- A6 ----------------------------------------------


def compute_proximity(
    quarantined: list[QuarantinedChunk],
    centroids: dict[int, np.ndarray],
) -> dict[UUID, dict]:
    """Per quarantined chunk: cosine similarity to top-K nearest active
    centroids, weighted by quality (floor at QUALITY_FLOOR)."""
    if not centroids:
        return {}
    labels = sorted(centroids.keys())
    mat = np.stack([centroids[l] for l in labels])  # [C, D]
    # L2-normalize centroids
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat_n = mat / norms

    out: dict[UUID, dict] = {}
    for q in quarantined:
        if q.embedding is None:
            continue
        v = q.embedding.astype(np.float32)
        vn = np.linalg.norm(v)
        if vn == 0:
            continue
        v = v / vn
        sims = mat_n @ v  # cosine similarities, [C]
        idx = int(np.argmax(sims))
        nearest_label = labels[idx]
        sim = float(sims[idx])
        distance = 1.0 - sim
        q_factor = max(0.0, q.quality - PROXIMITY_QUALITY_FLOOR) / (1.0 - PROXIMITY_QUALITY_FLOOR)
        weighted = max(0.0, sim) * q_factor
        out[q.id] = {
            "nearest_label": nearest_label,
            "nearest_distance": distance,
            "weighted_proximity": weighted,
        }
    return out


# --------------------- A7 ----------------------------------------------


def assemble_recovery(
    *,
    quarantined: list[QuarantinedChunk],
    matches: dict[UUID, list[dict]],
    slots: dict[tuple[UUID, int, int], dict],
    footprints: dict[int, ClusterFootprint],
    gaps: dict[UUID, int],
    proximity: dict[UUID, dict],
    commercial_labels: set[int],
    labeled_cluster_labels: set[int],
    chunk_to_cluster_t0: dict[UUID, int],
) -> list[dict]:
    rows: list[dict] = []
    for q in quarantined:
        # Entity component
        m = matches.get(q.id, [])
        if m:
            best = m[0]
            entity_score = float(best["similarity"])
            best_entity = best["entity"]
            best_sim = float(best["similarity"])
            best_frag = best["fragment"]
        else:
            entity_score = 0.0
            best_entity = None
            best_sim = None
            best_frag = None

        # Grid component — signed contribution.
        slot = slots.get((q.paper_id, q.page_sequence, q.position_bucket))
        if slot and slot.get("is_regular"):
            grid_label = slot["top_label"]
            grid_confidence = float(slot["signed_share"])  # +/- share
        else:
            grid_label = None
            grid_confidence = 0.0

        # Footprint component
        footprint_label = None
        footprint_score = 0.0
        slot_key = (q.paper_id, q.page_sequence, q.position_bucket)
        for label, fp in footprints.items():
            if q.lccn not in fp.lccns:
                continue
            if fp.date_min is None or q.date_issued < fp.date_min:
                continue
            if fp.date_max is None or q.date_issued > fp.date_max:
                continue
            if slot_key in fp.slot_keys:
                # Specific slot hit — strong
                cand_score = 1.0
            else:
                # Just paper + date window — weaker
                cand_score = 0.4
            if cand_score > footprint_score:
                footprint_score = cand_score
                footprint_label = label

        # Proximity component
        prox = proximity.get(q.id)
        if prox:
            nearest_label = prox["nearest_label"]
            nearest_distance = prox["nearest_distance"]
            weighted_proximity = prox["weighted_proximity"]
        else:
            nearest_label = None
            nearest_distance = None
            weighted_proximity = 0.0

        # Commerciality bias from the chunk's own cluster_t0,
        # independent of slot regularity. Catches commercial-cluster
        # chunks that the grid signal misses (e.g. sn83030313 page 4
        # buckets that don't cross the 0.60 regularity threshold).
        chunk_cluster = chunk_to_cluster_t0.get(q.id)
        if chunk_cluster is None or chunk_cluster < 0:
            commerciality_signal = 0.0
        elif chunk_cluster in commercial_labels:
            commerciality_signal = -1.0
        elif chunk_cluster in labeled_cluster_labels:
            commerciality_signal = +0.5
        else:
            commerciality_signal = 0.0

        # Grid contributes signed (commercial slots subtract).
        # Commerciality is the cluster-level analog — works whether
        # or not the chunk's slot is regular.
        relevance_prior = (
            W_ENTITY * entity_score
            + W_GRID * grid_confidence
            + W_FOOTPRINT * footprint_score
            + W_PROXIMITY * weighted_proximity
            + W_COMMERCIALITY * commerciality_signal
        )
        recoverability = max(0.0, min(1.0, q.quality))
        gap_label = gaps.get(q.id)
        # Gap bonus only for non-commercial clusters. A missing day in
        # steamboat schedules isn't a target a historian cares about —
        # doubling its score on every quarantined chunk in that slot
        # was the main reason the previous run's top-20 was wall-to-
        # wall commercial dark matter.
        gap_is_substantive = (
            gap_label is not None and gap_label not in commercial_labels
        )
        gap_bonus = GAP_BONUS if gap_is_substantive else 1.0
        # Recoverability is ADDITIVE (not a multiplier) — Checkpoint-1
        # showed the multiplicative form crushed every chunk on a
        # population with q≈0.01–0.04. max(0, …) floor so a strongly
        # negative grid/commerciality signal can pull a hopeless
        # candidate to zero without dragging the composite below.
        recovery_value = max(0.0, relevance_prior + W_RECOVER * recoverability) * gap_bonus

        rows.append({
            "chunk_id": q.id,
            "entity_match_score": entity_score,
            "best_entity": best_entity,
            "best_entity_similarity": best_sim,
            "best_entity_fragment": best_frag,
            "grid_section_guess": grid_label,
            "grid_confidence": grid_confidence,
            "grid_violation": False,  # populated downstream if you want per-chunk
            "footprint_score": footprint_score,
            "footprint_cluster_label": footprint_label,
            "gap_candidate_label": gap_label,  # resolved to cluster_id at write time
            "nearest_cluster_label": nearest_label,
            "nearest_distance": nearest_distance,
            "weighted_proximity": weighted_proximity,
            "commerciality_signal": commerciality_signal,
            "chunk_cluster_t0": chunk_cluster,
            "relevance_prior": relevance_prior,
            "recoverability": recoverability,
            "gap_bonus": gap_bonus,
            "recovery_value": recovery_value,
        })
    return rows


# --------------------- Writers -----------------------------------------


def _write_gazetteer(conn: psycopg.Connection, gazetteer: dict[str, dict]) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE entity_gazetteer")
        batch = 1000
        rows = list(gazetteer.items())
        for start in range(0, len(rows), batch):
            cur.executemany(
                """
                INSERT INTO entity_gazetteer
                  (surface, freq, cluster_t0, is_multiword)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (
                        surface, v["freq"], v["cluster_t0"], v["is_multiword"],
                    )
                    for surface, v in rows[start:start + batch]
                ],
            )


def _write_fragments(
    conn: psycopg.Connection,
    fragments: dict[UUID, list[tuple[str, str, int]]],
) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE quarantine_fragments")
        batch_rows: list[tuple] = []
        for cid, rows in fragments.items():
            for frag, kind, pos in rows:
                batch_rows.append((cid, frag, kind, pos))
        batch = 5000
        for start in range(0, len(batch_rows), batch):
            cur.executemany(
                """
                INSERT INTO quarantine_fragments
                  (chunk_id, fragment, kind, position)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chunk_id, fragment, position) DO NOTHING
                """,
                batch_rows[start:start + batch],
            )


def _write_matches(
    conn: psycopg.Connection,
    matches: dict[UUID, list[dict]],
) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE quarantine_entity_matches")
        rows: list[tuple] = []
        for cid, m in matches.items():
            for r in m:
                rows.append((cid, r["entity"], r["fragment"], r["similarity"], r["via"]))
        batch = 5000
        for start in range(0, len(rows), batch):
            cur.executemany(
                """
                INSERT INTO quarantine_entity_matches
                  (chunk_id, entity_surface, fragment, similarity, via_variant)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id, entity_surface, fragment)
                  DO UPDATE SET similarity = EXCLUDED.similarity,
                                via_variant = EXCLUDED.via_variant
                """,
                rows[start:start + batch],
            )


def _write_slots(
    conn: psycopg.Connection,
    slots: dict[tuple[UUID, int, int], dict],
) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE layout_slots")
        rows = [
            (
                s["paper_id"], s["page_sequence"], s["position_bucket"],
                s["top_label"], s["top_label_share"],
                s["top_content_type"], s["top_content_share"],
                s["sample_size"],
            )
            for s in slots.values()
        ]
        batch = 2000
        for start in range(0, len(rows), batch):
            cur.executemany(
                """
                INSERT INTO layout_slots
                  (paper_id, page_sequence, position_bucket,
                   top_label, top_label_share,
                   top_content_type, top_content_share, sample_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows[start:start + batch],
            )


def _write_chunk_recovery(
    conn: psycopg.Connection,
    per_chunk: list[dict],
) -> None:
    # Resolve gap_candidate_label → cluster_id once.
    cluster_id_by_label: dict[int, UUID] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, id FROM clusters
             WHERE run_id = (SELECT run_id FROM active_cluster_run WHERE singleton = true)
               AND tier = 0
            """
        )
        for label, cid in cur:
            cluster_id_by_label[int(label)] = cid if isinstance(cid, UUID) else UUID(str(cid))
    conn.commit()

    with conn.transaction():
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE chunk_recovery")
        rows = []
        for r in per_chunk:
            gap_label = r["gap_candidate_label"]
            gap_cid = cluster_id_by_label.get(gap_label) if gap_label is not None else None
            rows.append((
                r["chunk_id"],
                r["entity_match_score"],
                r["best_entity"],
                r["best_entity_similarity"],
                r["best_entity_fragment"],
                r["grid_section_guess"],
                r["grid_confidence"],
                r["grid_violation"],
                r["footprint_score"],
                r["footprint_cluster_label"],
                gap_cid,
                r["nearest_cluster_label"],
                r["nearest_distance"],
                r["weighted_proximity"],
                r["relevance_prior"],
                r["recoverability"],
                r["gap_bonus"],
                r["recovery_value"],
            ))
        batch = 2000
        for start in range(0, len(rows), batch):
            cur.executemany(
                """
                INSERT INTO chunk_recovery (
                  chunk_id,
                  entity_match_score, best_entity, best_entity_similarity, best_entity_fragment,
                  grid_section_guess, grid_confidence, grid_violation,
                  footprint_score, footprint_cluster_label, gap_candidate_cluster_id,
                  nearest_cluster_label, nearest_distance, weighted_proximity,
                  relevance_prior, recoverability, gap_bonus, recovery_value
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows[start:start + batch],
            )


# --------------------- Diagnostics -------------------------------------


def _lookup_paper_lccn_by_id(
    chunks: list[ActiveChunk],
) -> dict[UUID, str]:
    out: dict[UUID, str] = {}
    for ch in chunks:
        out[ch.paper_id] = ch.lccn
    return out


def _label_text_by_label(
    conn: psycopg.Connection, run_id: UUID,
    include_refusals: bool = False,
) -> dict[int, str]:
    """For tier-0 clusters in the active run, return {label: label_text}.

    By default, drops refusal strings ("cannot reliably identify…") so
    diagnostic tables stay readable. Pass include_refusals=True when
    the consumer just wants substring lookup over every available
    label (e.g. the commercial classifier needs to see every label
    even if it was a refusal — though refusals never match commercial
    keywords in practice)."""
    out: dict[int, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label, label_text FROM clusters WHERE run_id = %s AND tier = 0",
            (run_id,),
        )
        for label, text in cur:
            if not text:
                continue
            if include_refusals:
                out[int(label)] = text
                continue
            if len(text) <= 120 and "cannot reliably" not in text.lower():
                out[int(label)] = text
    conn.commit()
    return out


def classify_commercial(labels: dict[int, str]) -> set[int]:
    """Flag clusters whose label_text contains any COMMERCIAL_LABEL_KEYWORDS
    substring. Heuristic — unlabeled clusters default to non-commercial,
    which is the safe (don't deprioritize) default."""
    out: set[int] = set()
    for lab, text in labels.items():
        low = " " + text.lower() + " "
        for kw in COMMERCIAL_LABEL_KEYWORDS:
            if kw in low:
                out.add(int(lab))
                break
    return out


def _label_str(labels: dict[int, str], label: int | None) -> str:
    if label is None:
        return "—"
    text = labels.get(int(label))
    if text:
        return f"#{label} {text}"
    return f"#{label}"


def write_grid_report(
    slots: dict[tuple[UUID, int, int], dict],
    violations: list[dict],
    chunks: list[ActiveChunk],
    cluster_labels: dict[int, str],
    commercial_labels: set[int],
) -> None:
    paper_lccn = _lookup_paper_lccn_by_id(chunks)

    total = len(slots)
    regular = [s for s in slots.values() if s.get("is_regular")]
    reg_commercial = [s for s in regular if s.get("is_commercial")]
    reg_editorial = [s for s in regular if not s.get("is_commercial")]
    reg_frac = (len(regular) / total) if total else 0.0

    lines: list[str] = []
    lines.append("# Recovery: layout grid report")
    lines.append("")
    lines.append(f"Total slots: **{total:,}**")
    lines.append(
        f"Regular slots (share ≥ {GRID_REGULAR_THRESHOLD}, "
        f"sample ≥ {GRID_MIN_SAMPLES_FOR_REGULAR}): "
        f"**{len(regular):,}** ({reg_frac:.1%}) — "
        f"**{len(reg_commercial)} commercial** (negative grid sign), "
        f"**{len(reg_editorial)} editorial** (positive grid sign)."
    )
    lines.append("")
    lines.append("Grid contribution to relevance_prior:")
    lines.append("- regular + commercial → −share (deprioritize)")
    lines.append("- regular + editorial → +share (boost)")
    lines.append("- sub-threshold → 0")
    lines.append("")
    lines.append(f"## Top {GRID_REPORT_TOP_N} most regular slots")
    lines.append("")
    lines.append("| paper | page | bucket | top label | share | n | class | signed |")
    lines.append("| --- | ---: | ---: | --- | ---: | ---: | --- | ---: |")
    top = sorted(
        regular,
        key=lambda s: (-s["top_label_share"], -s["sample_size"]),
    )[:GRID_REPORT_TOP_N]
    for s in top:
        lccn = paper_lccn.get(s["paper_id"], "?")
        label_str = _label_str(cluster_labels, s["top_label"])
        cls = "commercial" if s["is_commercial"] else "editorial"
        signed = f"{s['signed_share']:+.2f}"
        lines.append(
            f"| {lccn} | {s['page_sequence']} | {s['position_bucket']} | "
            f"{label_str} | {s['top_label_share']:.2f} | {s['sample_size']} | "
            f"{cls} | {signed} |"
        )
    # Surface the commercial classifier so the operator can audit.
    if commercial_labels:
        commercial_with_text = sorted(
            [(lab, cluster_labels.get(lab, "")) for lab in commercial_labels
             if cluster_labels.get(lab)],
            key=lambda kv: kv[0],
        )
        lines.append("")
        lines.append(f"## Commercial classifier ({len(commercial_labels)} clusters flagged)")
        lines.append("")
        lines.append(
            f"Heuristic: label_text contains any of {list(COMMERCIAL_LABEL_KEYWORDS)}. "
            "Tunable at the top of scripts/recovery_score.py."
        )
        lines.append("")
        for lab, text in commercial_with_text[:40]:
            lines.append(f"- #{lab} — {text}")
        if len(commercial_with_text) > 40:
            lines.append(f"- … and {len(commercial_with_text) - 40} more.")
    lines.append("")
    lines.append(f"## Grid-violation days ({len(violations):,} total)")
    lines.append("")
    if not violations:
        lines.append("_No violations under current thresholds._")
    else:
        sample = sorted(
            violations, key=lambda v: (v["date"], v["lccn"], v["page_sequence"])
        )[:50]
        lines.append("Showing first 50 (sorted by date, paper, page).")
        lines.append("")
        lines.append("| date | paper | page | bucket | expected | actual |")
        lines.append("| --- | --- | ---: | ---: | --- | --- |")
        for v in sample:
            exp = _label_str(cluster_labels, v["expected_label"])
            act = _label_str(cluster_labels, v["actual_label"])
            lines.append(
                f"| {v['date']} | {v['lccn']} | {v['page_sequence']} | "
                f"{v['bucket']} | {exp} | {act} |"
            )
    (SCRIPT_DIR / "recovery_grid_report.md").write_text("\n".join(lines))


def write_fuzzy_samples(
    conn: psycopg.Connection,
    matches: dict[UUID, list[dict]],
) -> None:
    flat: list[tuple[UUID, dict]] = []
    for cid, ms in matches.items():
        for m in ms:
            flat.append((cid, m))
    if not flat:
        (SCRIPT_DIR / "recovery_fuzzy_samples.md").write_text(
            "# Recovery: fuzzy entity matches\n\nNo matches found.\n"
        )
        return

    # Stratified sample across similarity bands.
    high = [r for r in flat if r[1]["similarity"] >= 0.85]
    mid = [r for r in flat if 0.65 <= r[1]["similarity"] < 0.85]
    low = [r for r in flat if 0.55 <= r[1]["similarity"] < 0.65]
    rng = random.Random(42)

    def pick(pool, k):
        rng.shuffle(pool)
        return pool[:k]

    per_band = FUZZY_SAMPLES_N // 3
    sample = pick(high, per_band) + pick(mid, per_band) + pick(low, FUZZY_SAMPLES_N - 2 * per_band)
    sample.sort(key=lambda r: -r[1]["similarity"])

    # Fetch chunk context for the sample chunks.
    chunk_ids = list({cid for cid, _ in sample})
    excerpts: dict[UUID, str] = {}
    if chunk_ids:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content FROM chunks WHERE id = ANY(%s)",
                (chunk_ids,),
            )
            for cid, content in cur:
                cid_u = cid if isinstance(cid, UUID) else UUID(str(cid))
                excerpts[cid_u] = (content or "")[:240].replace("\n", " ")
        conn.commit()

    lines: list[str] = []
    lines.append("# Recovery: fuzzy entity matches (stratified sample)")
    lines.append("")
    lines.append(f"Sampled {len(sample)} of {len(flat):,} total matches across "
                 "high (≥0.85) / mid (0.65–0.85) / low (0.55–0.65) similarity bands.")
    lines.append("")
    for cid, m in sample:
        lines.append(
            f"- **{m['entity']}**  matched fragment **`{m['fragment']}`**  "
            f"sim={m['similarity']:.2f}  via={m['via']}"
        )
        snippet = excerpts.get(cid, "")
        if snippet:
            lines.append(f"  > _{snippet}_")
        lines.append("")
    (SCRIPT_DIR / "recovery_fuzzy_samples.md").write_text("\n".join(lines))


def write_top_candidates(
    per_chunk: list[dict],
    quarantined: list[QuarantinedChunk],
    cluster_labels: dict[int, str],
) -> None:
    by_id = {q.id: q for q in quarantined}
    ranked = sorted(per_chunk, key=lambda r: -r["recovery_value"])[:TOP_CANDIDATES_N]

    lines: list[str] = []
    lines.append(f"# Recovery: top {TOP_CANDIDATES_N} quarantined candidates")
    lines.append("")
    lines.append(
        "Composite = max(0, relevance_prior + W_RECOVER × recoverability) × gap_bonus. "
        f"Weights: entity={W_ENTITY}, grid={W_GRID}, footprint={W_FOOTPRINT}, "
        f"proximity={W_PROXIMITY}, commerciality={W_COMMERCIALITY}, "
        f"recover={W_RECOVER}. Gap bonus: {GAP_BONUS}× — fires ONLY when "
        "the gap cluster is non-commercial. Grid and commerciality are "
        "signed: negative for commercial clusters/slots."
    )
    lines.append("")
    for i, r in enumerate(ranked, 1):
        q = by_id.get(r["chunk_id"])
        if q is None:
            continue
        loc_url = (
            f"https://www.loc.gov/resource/{q.lccn}/{q.date_issued.isoformat()}"
            f"/ed-{q.edition}/seq-{q.page_sequence}/"
        )
        reasons: list[str] = []
        if r["gap_candidate_label"] is not None:
            reasons.append(
                f"gap candidate for {_label_str(cluster_labels, r['gap_candidate_label'])}"
            )
        if r["best_entity"]:
            reasons.append(
                f"fragment '{r['best_entity_fragment']}' ~ "
                f"{r['best_entity']} ({r['best_entity_similarity']:.2f})"
            )
        if r["grid_section_guess"] is not None:
            sign = "−" if r["grid_confidence"] < 0 else "+"
            reasons.append(
                f"grid {sign} {_label_str(cluster_labels, r['grid_section_guess'])} "
                f"({r['grid_confidence']:+.2f})"
            )
        if r["weighted_proximity"] > 0.1:
            reasons.append(
                f"semantic prox to {_label_str(cluster_labels, r['nearest_cluster_label'])} "
                f"({1.0 - (r['nearest_distance'] or 0):.2f})"
            )
        if r.get("commerciality_signal", 0.0) != 0.0:
            sign = "−" if r["commerciality_signal"] < 0 else "+"
            kind = "commercial cluster" if r["commerciality_signal"] < 0 else "editorial cluster"
            cluster_t0 = r.get("chunk_cluster_t0")
            if cluster_t0 is not None and cluster_t0 >= 0:
                reasons.append(
                    f"{kind} {sign} {_label_str(cluster_labels, cluster_t0)}"
                )
        reason_line = "; ".join(reasons) if reasons else "(only quality-weighted prior)"

        lines.append(f"### {i}. {q.lccn} {q.date_issued} p.{q.page_sequence} bucket {q.position_bucket}")
        lines.append("")
        lines.append(f"- recovery_value = **{r['recovery_value']:.4f}**")
        lines.append(
            f"  - relevance_prior = {r['relevance_prior']:+.4f}"
            f" (entity {r['entity_match_score']:+.2f}"
            f", grid {r['grid_confidence']:+.2f}"
            f", footprint {r['footprint_score']:+.2f}"
            f", proximity {r['weighted_proximity']:+.2f}"
            f", commerciality {r.get('commerciality_signal', 0.0):+.2f})"
        )
        lines.append(
            f"  - recoverability (quality, additive) = {r['recoverability']:.2f}"
            f"; gap_bonus = {r['gap_bonus']:.1f}×"
        )
        lines.append(f"- reason: {reason_line}")
        lines.append(f"- [LoC page]({loc_url})")
        # Show first 200 chars of the OCR'd content for sanity check.
        snippet = (q.content or "")[:200].replace("\n", " ")
        if snippet:
            lines.append(f"- OCR excerpt: `{snippet}…`")
        lines.append("")
    (SCRIPT_DIR / "recovery_top_candidates.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
