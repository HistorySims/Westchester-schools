"""PHASE 3 — Benchmark-driven tuning of the recovery-targeting heuristic.

Runs only after human approval of the Gate 2 baseline. Tunes OUR
heuristic's weights / thresholds / signals — it never trains a model,
never touches the live pipeline, and makes ZERO LLM calls.

The eval set is FROZEN: data/recovery_eval/*.jsonl.gz are the exact
tune/holdout files (with labels) the human reviewed at Gate 2. This
script re-derives every scoring signal from raw materials (garbled
content, stored gazetteer/matches/centroids) so each candidate config
can be evaluated apples-to-apples, but the labels and the split never
change.

Protocol (from the driving prompt):
  - A pre-registered sequence of candidate changes, each with a
    hypothesis, applied ONE at a time on top of the accepted config.
  - Accept iff TUNE precision@15 strictly improves (tie broken by
    P@20). Reject otherwise. Every iteration logged to tuning_log.md.
  - After every 3rd ACCEPTED change, peek at HOLDOUT P@15. If tune
    keeps rising while holdout falls below its previous checkpoint,
    STOP — overfitting — and revert to the config at the last good
    checkpoint.
  - Final numbers reported on HOLDOUT (the honest number). The tuned
    config is written to tuning_results.md for review — NOT applied
    to the live pipeline.

The candidate sequence encodes the four failure modes diagnosed in
the Gate 2 report (substring theme-cluster bug, common-surname entity
noise, token-split lexicon noise, grid executing true positives),
followed by new-signal candidates and weight sweeps.

Outputs: scripts/tuning_log.md, scripts/tuning_results.md.

Usage:
    uv run python scripts/recovery_tuning.py
"""

from __future__ import annotations

import difflib
import gzip
import json
import re
import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from american_stories_gate2 import (  # noqa: E402
    STRONG_TERMS,
    THEME_CLUSTER_KEYWORDS,
    THEME_ENTITIES,
)
from quarantine_by_cluster_refusal import is_refusal  # noqa: E402
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

from herald import settings  # noqa: E402


DATA_DIR = SCRIPT_DIR.parent / "data" / "recovery_eval"
TUNE_PATH = DATA_DIR / "recovery_eval_tune.jsonl.gz"
HOLDOUT_PATH = DATA_DIR / "recovery_eval_holdout.jsonl.gz"
LOG_PATH = SCRIPT_DIR / "tuning_log.md"
RESULTS_PATH = SCRIPT_DIR / "tuning_results.md"

P_PRIMARY = 15
P_SECONDARY = 20
HOLDOUT_CHECK_EVERY = 3       # accepted changes between holdout peeks

SHIPPING_NOISE_KEYWORDS = (
    "shipping", "maritime", "arrivals and departures", "arrivals at",
    "port of", "packet ship", "whaling", "whalemen", "vessels",
)

# ---- Baseline config (== the Gate 2 scorer, bit for bit) ---------------

BASELINE_CONFIG: dict = {
    # theme cluster selection
    "cluster_kw_word_boundary": False,
    "cluster_select_by_entities": False,
    # ranking
    "rank_raw_prior": False,       # baseline: max(0, prior) * gap
    # entity signal
    "entity_distinctive_only": False,
    "entity_rare_max_freq": 25,
    # lexicon signal
    "lex_phrase_only": False,
    "lex_fuzzy_min": 0.80,
    "lex_require_two_hits": False,
    "lex_density": False,
    # grid
    "grid_negative_scale": 1.0,
    # commerciality
    "shipping_is_noise": False,
    "labeled_bonus": 0.5,
    # extra signals (0 = off)
    "w_capital_density": 0.0,
    "w_numeric_density": 0.0,
    # gap
    "gap_enabled": True,
    # weights
    "w_entity": W_ENTITY,
    "w_grid": W_GRID,
    "w_footprint": W_FOOTPRINT,
    "w_proximity": W_PROXIMITY,
    "w_commerciality": W_COMMERCIALITY,
    "w_recover": W_RECOVER,
}

# ---- Pre-registered candidate changes, in order ------------------------
# (name, hypothesis, {config mutations})

CANDIDATES: list[tuple[str, str, dict]] = [
    ("cluster_kw_word_boundary",
     "Gate 2 showed 'deceased patRIOT' substring-matching the theme "
     "keyword 'riot', making the Andrew Jackson memorial cluster the "
     "ONLY theme cluster and poisoning entity/proximity. Word-boundary "
     "matching should remove the bogus cluster.",
     {"cluster_kw_word_boundary": True}),

    ("cluster_select_by_entities",
     "The Anti-Rent clusters may be labeled by people/places (Boughton, "
     "Steele, Andes, Delhi) rather than theme words, so keyword-only "
     "selection misses them. Also selecting clusters whose labels "
     "mention theme entities should recover the real reference set.",
     {"cluster_select_by_entities": True}),

    ("rank_raw_prior",
     "max(0, prior) collapses every negative-prior chunk into a tie at "
     "0.0, destroying rank order exactly where the grid penalty pushed "
     "true positives below zero. Ranking on the raw prior preserves "
     "the ordering evidence.",
     {"rank_raw_prior": True}),

    ("entity_distinctive_only",
     "Common surnames (Jackson, Smith, Steele-the-merchant) hit at "
     "sim=1.0 in every shipping manifest. Restricting the entity "
     "signal to multiword surfaces, rare surfaces (gazetteer freq ≤ "
     "25), or explicit theme entities should collapse the manifest "
     "false positives.",
     {"entity_distinctive_only": True}),

    ("lex_phrase_only",
     "Splitting multiword lexicon phrases into tokens let 'county' "
     "(from 'delaware county') and 'warrant' (from 'distress warrant') "
     "match generic text. Multiword phrases must match as phrases; "
     "only genuinely thematic single tokens stay in the fuzzy set.",
     {"lex_phrase_only": True}),

    ("lex_fuzzy_min_0_85",
     "difflib ratio 0.80 admits junk pairs on 5-6 letter words. "
     "Raising the floor to 0.85 trades a little recall on damaged "
     "spellings for precision.",
     {"lex_fuzzy_min": 0.85}),

    ("lex_require_two_hits",
     "One lexicon hit in a garbled chunk is often coincidence; two "
     "distinct hits rarely are. Full lexicon credit requires ≥2 "
     "distinct hits; a single hit earns half.",
     {"lex_require_two_hits": True}),

    ("grid_negative_scale_0_25",
     "The commercial-slot penalty is slot-level, but mixed pages carry "
     "editorial chunks in 'commercial' slots — Gate 2's worst false "
     "negatives all died to grid=-0.74. Scale negative grid to 25% so "
     "it demotes without executing.",
     {"grid_negative_scale": 0.25}),

    ("shipping_is_noise",
     "'Shipping arrivals and departures' clusters aren't ads, so they "
     "escaped the commercial classifier and collected +0.5 editorial "
     "boosts — but for ANY substantive research theme they're noise. "
     "Treat shipping/maritime clusters as commercial for theme scoring.",
     {"shipping_is_noise": True}),

    ("labeled_bonus_zero",
     "+0.5 for merely having a label boosts every manifest cluster. "
     "Reserve positive commerciality for actual theme clusters.",
     {"labeled_bonus": 0.0}),

    ("capital_density_penalty",
     "Shipping manifests and hotel registers are wall-to-wall "
     "capitalized names; news prose is not. A negative weight on "
     "capitalized-token density should suppress list-like chunks "
     "without touching prose.",
     {"w_capital_density": -0.20}),

    ("numeric_density_penalty",
     "Price tables and manifests are digit-dense; editorial text is "
     "not. Penalize digit density.",
     {"w_numeric_density": -0.20}),

    ("lex_density",
     "Max-similarity saturates at one lucky hit; counting DISTINCT "
     "lexicon hits (capped at 3) rewards chunks with converging "
     "evidence.",
     {"lex_density": True}),

    ("w_entity_0_50",
     "If the distinctive-entity gate landed, entity matches are now "
     "high-precision and deserve more weight.",
     {"w_entity": 0.50}),

    ("w_proximity_0_45",
     "With real theme clusters selected, embedding proximity should "
     "carry more of the ranking than it could at baseline.",
     {"w_proximity": 0.45}),

    ("w_recover_zero",
     "Quality correlates with clean-but-boring list pages in this "
     "corpus; as a ranking term it may hurt theme precision.",
     {"w_recover": 0.0}),

    ("gap_off",
     "Gap candidacy contributed nothing but commercial noise at Gate "
     "2 (every gap was a schedule/medical cluster). Test removing the "
     "bonus entirely for theme scoring.",
     {"gap_enabled": False}),

    ("grid_negative_zero",
     "If 0.25 scaling helped, maybe the negative grid contributes "
     "nothing at all for theme scoring — chunk-level commerciality "
     "already covers it.",
     {"grid_negative_scale": 0.0}),

    ("w_entity_0_20",
     "Counter-sweep: if boosting entity weight hurt or did nothing, "
     "try reducing it below baseline in case lexicon+proximity are "
     "the real signal.",
     {"w_entity": 0.20}),
]


# ======================= Context (loaded once) ==========================


class Ctx:
    """Immutable raw materials shared by every config evaluation."""

    def __init__(self) -> None:
        self.rows_tune: list[dict] = []
        self.rows_holdout: list[dict] = []
        self.clusters: dict[int, dict] = {}          # label → {text, centroid_n}
        self.refused: set[int] = set()
        self.gazetteer: dict[str, dict] = {}         # surface → {freq, multi, clusters}
        self.matches: dict[str, list[tuple[str, float]]] = {}  # chunk → [(surface, sim)]
        self.fragments: dict[str, list[str]] = {}    # chunk → [frag lower]
        self.embeddings: dict[str, np.ndarray] = {}  # chunk → unit vector
        self.capital_density: dict[str, float] = {}
        self.numeric_density: dict[str, float] = {}
        # lexicon precomputation
        self.frag_best_tokens: dict[str, float] = {}   # frag → best ratio (token variants)
        self.frag_best_phrases: dict[str, float] = {}  # frag → best ratio (phrase-only set)
        self.chunk_phrase_hits: dict[str, int] = {}    # multiword phrase hits in raw content
        self._theme_cache: dict[tuple, tuple[frozenset, frozenset]] = {}

    # ---- theme cluster + commercial sets, cached per relevant flags ----

    def theme_and_commercial(self, cfg: dict) -> tuple[frozenset, frozenset]:
        key = (cfg["cluster_kw_word_boundary"],
               cfg["cluster_select_by_entities"],
               cfg["shipping_is_noise"])
        got = self._theme_cache.get(key)
        if got is not None:
            return got

        theme: set[int] = set()
        for lab, c in self.clusters.items():
            if lab in self.refused or not c["text"]:
                continue
            low = " " + c["text"].lower() + " "
            hit = False
            for kw in THEME_CLUSTER_KEYWORDS:
                if cfg["cluster_kw_word_boundary"]:
                    if _term_re(kw).search(low):
                        hit = True
                        break
                elif kw in low:
                    hit = True
                    break
            if not hit and cfg["cluster_select_by_entities"]:
                for ent in THEME_ENTITIES:
                    if _term_re(ent).search(low):
                        hit = True
                        break
            if hit:
                theme.add(lab)

        labels_by_t0 = {lab: c["text"] for lab, c in self.clusters.items()
                        if c["text"]}
        commercial = set(classify_commercial(labels_by_t0)) - self.refused
        if cfg["shipping_is_noise"]:
            for lab, text in labels_by_t0.items():
                low = text.lower()
                if any(kw in low for kw in SHIPPING_NOISE_KEYWORDS):
                    commercial.add(lab)
        commercial -= theme

        got = (frozenset(theme), frozenset(commercial))
        self._theme_cache[key] = got
        return got

    def theme_centroids(self, theme: frozenset) -> np.ndarray | None:
        vecs = [self.clusters[l]["centroid_n"] for l in sorted(theme)
                if self.clusters[l]["centroid_n"] is not None]
        return np.stack(vecs) if vecs else None


_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _term_re(term: str) -> re.Pattern:
    pat = _WORD_RE_CACHE.get(term)
    if pat is None:
        pat = re.compile(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])")
        _WORD_RE_CACHE[term] = pat
    return pat


# ======================= Loading ========================================


def load_ctx() -> Ctx:
    ctx = Ctx()
    ctx.rows_tune = _read_jsonl(TUNE_PATH)
    ctx.rows_holdout = _read_jsonl(HOLDOUT_PATH)
    print(f"Frozen eval set: tune={len(ctx.rows_tune)}, "
          f"holdout={len(ctx.rows_holdout)}")

    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)
    conn = psycopg.connect(cfg.supabase_db_url, autocommit=False,
                           prepare_threshold=None)
    register_vector(conn)
    try:
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
            for label, text, ac, c in cur.fetchall():
                vec = ac if ac is not None else c
                if vec is not None:
                    v = np.asarray(vec, dtype=np.float32)
                    n = np.linalg.norm(v)
                    v = v / n if n > 0 else None
                else:
                    v = None
                ctx.clusters[int(label)] = {"text": text, "centroid_n": v}
        conn.commit()
        ctx.refused = {lab for lab, c in ctx.clusters.items()
                       if is_refusal(c["text"])}

        with conn.cursor() as cur:
            cur.execute(
                "SELECT surface, freq, is_multiword, cluster_t0 "
                "FROM entity_gazetteer"
            )
            for surface, freq, multi, labs in cur.fetchall():
                ctx.gazetteer[surface] = {
                    "freq": int(freq),
                    "multi": bool(multi),
                    "clusters": set(labs or []),
                }
        conn.commit()

        with conn.cursor(name="t_matches") as cur:
            cur.itersize = 5000
            cur.execute(
                "SELECT chunk_id, entity_surface, similarity "
                "FROM quarantine_entity_matches"
            )
            for cid, surface, sim in cur:
                ctx.matches.setdefault(str(cid), []).append(
                    (surface, float(sim)))
        conn.commit()

        with conn.cursor(name="t_frags") as cur:
            cur.itersize = 5000
            cur.execute(
                "SELECT chunk_id, fragment FROM quarantine_fragments"
            )
            for cid, frag in cur:
                ctx.fragments.setdefault(str(cid), []).append(frag.lower())
        conn.commit()

        eval_ids = [r["chunk_id"] for r in ctx.rows_tune + ctx.rows_holdout]
        batch = 500
        with conn.cursor() as cur:
            for i in range(0, len(eval_ids), batch):
                ids = [UUID(c) for c in eval_ids[i:i + batch]]
                cur.execute(
                    "SELECT id, embedding FROM chunks "
                    "WHERE id = ANY(%s) AND embedding IS NOT NULL",
                    (ids,),
                )
                for cid, vec in cur.fetchall():
                    v = np.asarray(vec, dtype=np.float32)
                    n = np.linalg.norm(v)
                    if n > 0:
                        ctx.embeddings[str(cid)] = v / n
        conn.commit()
    finally:
        conn.close()

    print(f"Loaded {len(ctx.clusters)} clusters ({len(ctx.refused)} refused), "
          f"{len(ctx.gazetteer):,} gazetteer entries, "
          f"matches for {len(ctx.matches):,} chunks, "
          f"fragments for {len(ctx.fragments):,} chunks, "
          f"{len(ctx.embeddings):,} embeddings")

    _precompute_text_signals(ctx)
    _precompute_lexicon(ctx)
    return ctx


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _precompute_text_signals(ctx: Ctx) -> None:
    tok_re = re.compile(r"[A-Za-z][A-Za-z'\-]+")
    for r in ctx.rows_tune + ctx.rows_holdout:
        cid, content = r["chunk_id"], r.get("content", "")
        toks = tok_re.findall(content)
        caps = sum(1 for t in toks if t[0].isupper())
        ctx.capital_density[cid] = caps / max(1, len(toks))
        no_space = content.replace(" ", "").replace("\n", "")
        digits = sum(1 for ch in no_space if ch.isdigit())
        ctx.numeric_density[cid] = digits / max(1, len(no_space))


def _precompute_lexicon(ctx: Ctx) -> None:
    """Two fragment→best-ratio maps (token-split vs phrase-only sets)
    plus per-chunk multiword-phrase substring hits."""
    single_terms = [t for t in (*STRONG_TERMS, *THEME_ENTITIES)
                    if " " not in t and len(t) >= 4]
    token_split = {tok for t in (*STRONG_TERMS, *THEME_ENTITIES)
                   for tok in t.split() if len(tok) >= 4}

    def variant_set(terms) -> set[str]:
        out: set[str] = set()
        for t in terms:
            out |= {v.lower() for v in damage_variants(t)}
        return out

    variants_tokens = variant_set(token_split)
    variants_phrase = variant_set(single_terms)

    all_frags = sorted({f for frags in ctx.fragments.values() for f in frags})
    by_len_tok = defaultdict(list)
    by_len_phr = defaultdict(list)
    for v in variants_tokens:
        by_len_tok[len(v)].append(v)
    for v in variants_phrase:
        by_len_phr[len(v)].append(v)

    def best_ratio(frag: str, by_len: dict) -> float:
        if any(frag in by_len[n] for n in (len(frag),) if n in by_len):
            return 1.0
        best = 0.0
        if len(frag) < 5:
            return 0.0
        for n in range(len(frag) - 2, len(frag) + 3):
            for v in by_len.get(n, ()):
                r = difflib.SequenceMatcher(None, frag, v).ratio()
                if r > best:
                    best = r
                    if best >= 0.99:
                        return best
        return best

    for i, frag in enumerate(all_frags):
        ctx.frag_best_tokens[frag] = best_ratio(frag, by_len_tok)
        ctx.frag_best_phrases[frag] = best_ratio(frag, by_len_phr)
        if i and i % 5000 == 0:
            print(f"  lexicon precompute {i:,}/{len(all_frags):,} fragments")

    multiword = [t for t in (*STRONG_TERMS, *THEME_ENTITIES) if " " in t]
    for r in ctx.rows_tune + ctx.rows_holdout:
        low = r.get("content", "").lower()
        ctx.chunk_phrase_hits[r["chunk_id"]] = sum(
            1 for t in multiword if t in low)


# ======================= Scoring under a config =========================


def score_rows(rows: list[dict], cfg: dict, ctx: Ctx) -> None:
    theme, commercial = ctx.theme_and_commercial(cfg)
    mat = ctx.theme_centroids(theme)

    # Theme-eligible entity surfaces under this config.
    def entity_ok(surface: str) -> bool:
        g = ctx.gazetteer.get(surface)
        if g is None:
            return False
        theme_linked = bool(g["clusters"] & theme) or any(
            _term_re(e).search(surface.lower()) for e in THEME_ENTITIES)
        if not theme_linked:
            return False
        if not cfg["entity_distinctive_only"]:
            return True
        if g["multi"] or g["freq"] <= cfg["entity_rare_max_freq"]:
            return True
        return any(_term_re(e).search(surface.lower())
                   for e in THEME_ENTITIES)

    frag_best = (ctx.frag_best_phrases if cfg["lex_phrase_only"]
                 else ctx.frag_best_tokens)

    for r in rows:
        cid = r["chunk_id"]

        # --- lexicon
        hits = [f for f in ctx.fragments.get(cid, ())
                if frag_best.get(f, 0.0) >= cfg["lex_fuzzy_min"]]
        n_hits = len(set(hits)) + ctx.chunk_phrase_hits.get(cid, 0)
        if n_hits == 0:
            s_lex = 0.0
        elif cfg["lex_density"]:
            s_lex = min(1.0, n_hits / 3.0)
        else:
            s_lex = 1.0
            if cfg["lex_require_two_hits"] and n_hits < 2:
                s_lex = 0.5

        # --- entity
        s_ent = 0.0
        for surface, sim in ctx.matches.get(cid, ()):
            if sim > s_ent and entity_ok(surface):
                s_ent = sim
        entity_term = max(s_lex, s_ent)

        # --- grid
        gg = r.get("grid_guess")
        gc = float(r.get("grid_conf") or 0.0)
        if gg is None:
            s_grid = 0.0
        elif gg in theme:
            s_grid = abs(gc)
        elif gg in commercial:
            s_grid = -abs(gc) * cfg["grid_negative_scale"]
        else:
            s_grid = 0.0

        # --- footprint
        s_fp = (float(r.get("footprint_score") or 0.0)
                if r.get("footprint_label") in theme else 0.0)

        # --- proximity
        s_prox = 0.0
        v = ctx.embeddings.get(cid)
        if mat is not None and v is not None:
            sim = float(np.max(mat @ v))
            qf = max(0.0, r["quality"] - PROXIMITY_QUALITY_FLOOR) \
                / (1.0 - PROXIMITY_QUALITY_FLOOR)
            s_prox = max(0.0, sim) * qf

        # --- commerciality
        c0 = r.get("cluster_t0", -1)
        if c0 in commercial:
            s_comm = -1.0
        elif c0 in theme:
            s_comm = 1.0
        elif (c0 is not None and c0 >= 0
              and ctx.clusters.get(c0, {}).get("text")
              and c0 not in ctx.refused):
            s_comm = cfg["labeled_bonus"]
        else:
            s_comm = 0.0

        gap = (GAP_BONUS if cfg["gap_enabled"]
               and r.get("gap_label") in theme else 1.0)

        prior = (cfg["w_entity"] * entity_term
                 + cfg["w_grid"] * s_grid
                 + cfg["w_footprint"] * s_fp
                 + cfg["w_proximity"] * s_prox
                 + cfg["w_commerciality"] * s_comm
                 + cfg["w_recover"] * r["quality"]
                 + cfg["w_capital_density"] * ctx.capital_density.get(cid, 0.0)
                 + cfg["w_numeric_density"] * ctx.numeric_density.get(cid, 0.0))

        r["_score"] = (prior * gap if cfg["rank_raw_prior"]
                       else max(0.0, prior) * gap)


def p_at(rows: list[dict], k: int) -> float:
    ranked = sorted(rows, key=lambda r: (-r["_score"], r["chunk_id"]))[:k]
    return sum(r["label"] for r in ranked) / max(1, len(ranked))


def evaluate(rows: list[dict], cfg: dict, ctx: Ctx) -> tuple[float, float]:
    score_rows(rows, cfg, ctx)
    return p_at(rows, P_PRIMARY), p_at(rows, P_SECONDARY)


# ======================= Tuning loop ====================================


def main() -> None:
    ctx = load_ctx()

    log: list[str] = ["# Phase 3 tuning log", ""]
    cfg = deepcopy(BASELINE_CONFIG)

    tune_p15, tune_p20 = evaluate(ctx.rows_tune, cfg, ctx)
    hold_p15, hold_p20 = evaluate(ctx.rows_holdout, cfg, ctx)
    baseline = {"tune_p15": tune_p15, "tune_p20": tune_p20,
                "hold_p15": hold_p15, "hold_p20": hold_p20}
    log.append(f"Baseline (recomputed in-harness): TUNE P@15={tune_p15:.3f} "
               f"P@20={tune_p20:.3f} · HOLDOUT P@15={hold_p15:.3f} "
               f"P@20={hold_p20:.3f}")
    log.append("")
    print(log[-2])

    accepted: list[tuple[str, float, float, str]] = []
    checkpoints: list[dict] = [{
        "n_accepted": 0, "tune_p15": tune_p15, "hold_p15": hold_p15,
        "cfg": deepcopy(cfg),
    }]
    overfit_stop: str | None = None

    for name, hypothesis, mutation in CANDIDATES:
        trial = deepcopy(cfg)
        trial.update(mutation)
        t15, t20 = evaluate(ctx.rows_tune, trial, ctx)

        better = t15 > tune_p15 or (t15 == tune_p15 and t20 > tune_p20)
        decision = "ACCEPT" if better else "reject"
        log.append(f"## {len(accepted) + 1 if better else '—'} · {name} "
                   f"[{decision}]")
        log.append("")
        log.append(f"_Hypothesis:_ {hypothesis}")
        log.append("")
        log.append(f"TUNE P@15 {tune_p15:.3f} → {t15:.3f} · "
                   f"P@20 {tune_p20:.3f} → {t20:.3f}")
        log.append("")
        print(f"{decision:6s} {name}: P@15 {tune_p15:.3f} → {t15:.3f}")

        if not better:
            continue

        cfg, tune_p15, tune_p20 = trial, t15, t20
        accepted.append((name, t15, t20, hypothesis))

        if len(accepted) % HOLDOUT_CHECK_EVERY == 0:
            h15, _h20 = evaluate(ctx.rows_holdout, cfg, ctx)
            prev = checkpoints[-1]
            log.append(f"**Holdout checkpoint** after {len(accepted)} "
                       f"accepted: HOLDOUT P@15 {prev['hold_p15']:.3f} → "
                       f"{h15:.3f}")
            log.append("")
            print(f"  holdout checkpoint: {prev['hold_p15']:.3f} → {h15:.3f}")
            if h15 < prev["hold_p15"] and tune_p15 > prev["tune_p15"]:
                overfit_stop = (
                    f"OVERFITTING at {len(accepted)} accepted changes: tune "
                    f"P@15 rose {prev['tune_p15']:.3f}→{tune_p15:.3f} while "
                    f"holdout fell {prev['hold_p15']:.3f}→{h15:.3f}. "
                    f"Reverted to the previous checkpoint config.")
                log.append(f"**{overfit_stop}**")
                log.append("")
                cfg = deepcopy(prev["cfg"])
                tune_p15, tune_p20 = evaluate(ctx.rows_tune, cfg, ctx)
                break
            checkpoints.append({
                "n_accepted": len(accepted), "tune_p15": tune_p15,
                "hold_p15": h15, "cfg": deepcopy(cfg),
            })

    # ---- Final honest numbers on HOLDOUT ------------------------------
    final_h15, final_h20 = evaluate(ctx.rows_holdout, cfg, ctx)
    final_t15, final_t20 = evaluate(ctx.rows_tune, cfg, ctx)

    LOG_PATH.write_text("\n".join(log))

    results: list[str] = ["# Phase 3 tuning results", ""]
    results.append("| metric | baseline | final |")
    results.append("| --- | ---: | ---: |")
    results.append(f"| HOLDOUT P@15 (honest) | {baseline['hold_p15']:.3f} | "
                   f"**{final_h15:.3f}** |")
    results.append(f"| HOLDOUT P@20 | {baseline['hold_p20']:.3f} | "
                   f"{final_h20:.3f} |")
    results.append(f"| TUNE P@15 | {baseline['tune_p15']:.3f} | "
                   f"{final_t15:.3f} |")
    results.append(f"| TUNE P@20 | {baseline['tune_p20']:.3f} | "
                   f"{final_t20:.3f} |")
    results.append("")
    if overfit_stop:
        results.append(f"**{overfit_stop}**")
        results.append("")
    results.append(f"## Accepted changes ({len(accepted)})")
    results.append("")
    if accepted:
        for i, (name, t15, t20, hyp) in enumerate(accepted, 1):
            results.append(f"{i}. **{name}** → tune P@15 {t15:.3f} — {hyp}")
    else:
        results.append("_None — no candidate improved tune P@15._")
    results.append("")
    results.append("## What mattered (plain English)")
    results.append("")
    results.extend(_narrative(accepted, baseline, final_h15))
    results.append("")
    results.append("## Final configuration (NOT applied to the live pipeline)")
    results.append("")
    results.append("```json")
    results.append(json.dumps(cfg, indent=2))
    results.append("```")
    results.append("")
    results.append("Review required before any of this touches "
                   "scripts/recovery_score.py or the brief route: a heuristic "
                   "tuned on one theme (political violence) may not "
                   "generalize to other research topics.")
    RESULTS_PATH.write_text("\n".join(results))

    print(f"\nFinal HOLDOUT P@15: {baseline['hold_p15']:.3f} → {final_h15:.3f}")
    print(f"Wrote {LOG_PATH} and {RESULTS_PATH}")


def _narrative(accepted, baseline, final_h15) -> list[str]:
    out: list[str] = []
    if not accepted:
        out.append("No change improved tune precision — the baseline's "
                   "failure modes may need signal redesign rather than "
                   "reweighting. See the per-iteration log.")
        return out
    names = {n for n, *_ in accepted}
    delta = final_h15 - baseline["hold_p15"]
    out.append(f"Holdout precision@15 moved {baseline['hold_p15']:.3f} → "
               f"{final_h15:.3f} ({delta:+.3f}).")
    if {"cluster_kw_word_boundary", "cluster_select_by_entities"} & names:
        out.append("- Fixing WHICH clusters count as the theme (word "
                   "boundaries; entity-bearing labels) mattered before any "
                   "weight did — the reference frame drives every signal.")
    if "entity_distinctive_only" in names:
        out.append("- Gating entities to distinctive surfaces (multiword / "
                   "rare / listed) removed the common-surname noise that "
                   "made manifests outrank news.")
    if {"lex_phrase_only", "lex_fuzzy_min_0_85", "lex_require_two_hits",
            "lex_density"} & names:
        out.append("- Lexicon precision (phrase-level matching, higher fuzzy "
                   "floor, multi-hit evidence) beat lexicon recall for "
                   "top-of-ranking quality.")
    if {"grid_negative_scale_0_25", "grid_negative_zero",
            "rank_raw_prior"} & names:
        out.append("- The slot-level commercial penalty was killing true "
                   "positives; softening it (and ranking on the raw prior "
                   "instead of a zero floor) recovered them.")
    if {"shipping_is_noise", "labeled_bonus_zero", "capital_density_penalty",
            "numeric_density_penalty"} & names:
        out.append("- List-like content (shipping, registers, tables) needed "
                   "explicit suppression — cluster labels alone don't mark "
                   "it commercial.")
    return out


if __name__ == "__main__":
    main()
