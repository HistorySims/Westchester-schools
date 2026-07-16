# Herald (newspaper engine) — origin status

> **Note (2026-07):** This is the status of the *original* Herald — the
> 1840s-newspaper research tool this project was forked from. We kept the
> `herald` name to remember those roots, but the current project is the
> **Westchester schools** corpus. For *this* project's live status see
> [`STATUS.md`](STATUS.md). This file is retained because the engine
> design and the postmortems below (OCR quarantine, the recovery
> answer-key experiment) are the lineage the schools engine inherits.

*Last updated: July 2026*

A snapshot of what Herald actually does today. Supersedes the Phase 1/2
plan docs and the June 2026 status. This revision adds the Research
Brief, the Cluster Dossier (the "geometry of a story" work), the
OCR-quality quarantine pipeline, and a full write-up of the
quarantine-recovery effort — including the parts that didn't pan out,
recorded here so we don't re-run them.

---

## What Herald is

A semantic-research tool over 1840s New York newspapers — the
New-York Daily Tribune (`sn83030213`) and the New York Herald
(`sn83030313`), summer 1845. (The Albany Evening Journal appeared in
earlier corpora; the working set is now the two NY papers. Sparse 1842
sample data was deleted long ago to free space — see the date-range
note under "False starts.") ~20,900 current chunks after the
quarantine pass.

Three surfaces, in the order a researcher moves through them:

### `/brief` — Research Brief (added since last status)
A deliberate, button-driven action, not a chat. The historian types a
plain-English question; Herald returns an organized finding aid.

- **Translation pass** — Haiku (temp 0) rewrites the modern question
  into 1840s newspaper diction: period terms, likely entities,
  candidate date ranges, search phrases, a neutral restatement.
- **Cluster matching** — Voyage embeds the restated question + phrases;
  cosine-matches against fine-cluster (tier-0) centroids; separately
  tallies FTS hits per cluster; blends the two into one relevance
  score per cluster (weights are tunable constants). Top clusters roll
  up via `parent_id` to show the broader theme each sits under.
- **Geometry cards** — each matched cluster is presented through the
  metrics we already store: label, size (active/stored), date range,
  peak week, burstiness, net drift + directionality ratio, a
  per-week volume sparkline, contributing-paper proportions, and a
  one-line **shape tag** (Directional evolving / Spike-and-decay /
  Heartbeat / Brief mention / Churn / Topical thread) derived from
  percentile thresholds against the corpus distribution.
- **Honesty guard** — if the top relevance is weak, the brief says so
  ("your question matches this corpus weakly…") instead of fabricating
  confidence. Refusal-string cluster labels are scrubbed at the API
  boundary and again in the UI.
- Persists in `localStorage` so navigating to a dossier and back
  doesn't lose the brief.

### `/cluster/[id]` — Cluster Dossier (added since last status)
The connective tissue between the brief, the map, and the LoC page
images. One page per cluster, mobile-first, dark theme. Two zones:

- **Anatomy panel** — three bands on one shared time axis, driven by a
  single scrubber: (1) a per-paper streamgraph of weekly volume;
  (2) a "comet trail" of weekly centroids in UMAP space over a faint
  member point-cloud, colored pale→saturated by time, with the net
  drift / direction ratio shown numerically beside it; (3) a per-week
  "word river" of c-TF-IDF terms (this week vs. the cluster's other
  weeks). Color = paper; opacity = OCR quality throughout.
- **Evidence feed** — every active chunk, chronological, sticky week
  dividers, each card an index entry (muted "OCR excerpt", opacity by
  quality) with a prominent tap-through to the LoC page image. Star
  toggles (session-local) and a markdown citation export.
- Scrubber ↔ feed are linked both ways (scroll-spy with a guard so the
  smooth-scroll doesn't rebound). Zero-active clusters render an empty
  state rather than a broken page.

### `/explore` — Cluster map (largely as before)
- `deck.gl` WebGL scatter of all chunks, UMAP-projected, colored by
  cluster at the selected tier.
- Four hierarchical tiers (HDBSCAN leaf + size-weighted agglomerative
  merge), Bursty Topics sidebar, cluster stories (cached Sonnet
  summaries), map search, date-window scrubbing, content filters.
- **Timeline minimap** (added since last status) — a vertical
  "barcode" of every chunk, per-paper columns on a synchronized date
  axis, Morse-code cell pattern encoding OCR quality (more gaps = worse
  OCR), gold end-caps + inward arrows on search hits. Momentum
  ("toss") scrolling on touch and mouse. Available as a mobile tab
  alongside the map.
- Chunk detail panel now links out to the cluster's dossier.

---

## Data model additions since last status

- **`chunks.status` / `quality_score` / `quality_subscores` /
  `quarantine_reason`** — OCR quality scoring (migration 0006). See
  the quarantine section below.
- **`clusters` geometry columns** — `drift_cumulative`, `drift_net`,
  `drift_weeks` (0007); `active_size`, `active_centroid`,
  `burstiness`, `active_date_min/max` (0008), all recomputed from
  active chunks only.
- **`cluster_weeks`** (0009) — per-cluster per-week aggregates
  (counts, paper mix, mean OCR quality, UMAP centroid, top terms) that
  back the dossier anatomy panel. Weekly centroids are the *mean of
  member UMAP coordinates* (mean-of-projections), deliberately — no
  UMAP-model persistence, visually consistent with the map.
- **Quarantine-recovery tables** (0010) — `entity_gazetteer`,
  `quarantine_fragments`, `quarantine_entity_matches`, `layout_slots`,
  `chunk_recovery`. Built, populated, and diagnostically useful; see
  the recovery postmortem for why they are **not** wired into a live
  surface.

Everything downstream (brief matching, dossier metrics, drift, shape
tags) operates on `status='active'` chunks only, consistent across the
pipeline.

---

## OCR quality + quarantine

1840s microfilm OCR ranges from clean to unreadable. We score every
chunk and hide the unreadable ones from retrieval so RAG stops
skimming garbage.

- **Per-chunk heuristic** (`src/herald/classify.py`) — `dict_word_ratio`
  is the primary signal, plus structural checks. Two-stage bar:
  `< 0.18` → quarantine unconditionally; `< 0.28` + structurally weak →
  quarantine; `< 0.40` → active but flagged `reassignment_candidate`;
  else active. (The original single-stage bar required *both* low dict
  ratio *and* structural breakage — far too lenient; it quarantined
  only 3 chunks in the whole corpus. See false starts.)
- **Cluster-level corrective** — chunks in a fine cluster whose Haiku
  label is itself a refusal ("I cannot reliably identify a shared
  topic — the text appears corrupted…") are quarantined as
  `cluster_refused`. This trusts Haiku's read of the cluster's
  representative chunks. Re-scoring preserves this flag (union
  semantics: either signal quarantines).
- **Framing** — "quarantined" means *unreadable enough that machine
  retrieval returns noise*, not *lost*. Every quarantined chunk keeps
  its LoC page link; a historian can always click through to the
  image. The 0.18–0.40 band is named `reassignment_candidate` (not
  "recovery"): enough legible text to stay active, but noisy enough
  that its cluster placement may be wrong.
- Current split: ~1,382 quarantined (`ocr_illegible` + `too_short` +
  `cluster_refused`), ~7,959 reassignment candidates, ~11,559 clean.

---

## Pipeline architecture (unchanged fundamentals)

- **Ingest** — `herald ingest`, loc.gov JSON API, Voyage `voyage-3.5`
  (1024-dim), Supabase Postgres. Now **fails fast** on LoC 429 / HTML
  interstitial rather than retrying into a deeper block (raises
  `LOCBlocked`).
- **Retrieval** — hybrid Voyage semantic + Postgres FTS, RRF (k=60),
  Voyage `rerank-2.5`, MMR for breadth queries, 12 chunks to Sonnet.
  RPCs filter `status='active'`.
- **Clustering** — HDBSCAN leaf on raw 1024-dim vectors, agglomerative
  merge with size-weighted centroids to 4 tiers, UMAP to 2D, Haiku
  auto-labels (rate-limit-paced). A **recompute + relabel** job
  refreshes all geometry (active centroids, drift, cluster_weeks) and
  labels after each quarantine change.
- **Synthesis** — Claude Sonnet 4.6, citation validator with one retry.

All batch jobs run as GitHub Actions `workflow_dispatch` workflows
(the sandbox and Vercel can't reach LoC or open direct Postgres
connections; Actions can reach Supabase).

---

## What's blocked

**Library of Congress is behind Cloudflare bot protection**, blocking
every cloud-IP path (ingest 403, image proxy 502, inline `<img>`,
iframe via `X-Frame-Options: DENY`). Confirmed *not* our code — the
May-23 client reproduces the same 403. We emailed LoC Labs; they asked
for a blocked-URL example with timestamp, which we provided (a
2026-06-06 403 on the JSON collection endpoint from a GitHub Actions
IP). Awaiting reply. The page viewer is an honest "Open on Library of
Congress" card; users solve the challenge once per tab. Corpus
expansion and any vision re-OCR are gated on this.

---

## What grew beyond plan (cumulative)

- The entire `/explore` page (hierarchical clustering, burstiness,
  weighted centroids, cluster stories, map search, timeline minimap).
- Response modes (Synthesis / Research / Directory).
- **Research Brief** — question → translation → cluster match →
  geometry cards → Sonnet orientation.
- **Cluster Dossier** — the "geometry of a story": streamgraph, comet
  trail, word river, evidence feed, citation export.
- **Semantic drift metrics** — cumulative vs. net centroid travel per
  cluster, and the net/cum directionality ratio that separates
  evolving stories from churn.
- **OCR quarantine pipeline** — quality scoring, two-stage bar,
  cluster-refusal corrective.

---

## False starts and dead ends (kept so we don't repeat them)

- **Stale 1842 dates in the UI** — cluster date ranges showed 1842
  even though 1842 chunks were deleted. Cause: `clusters.date_min/max`
  were stored at cluster-run time and never refreshed after the FK
  cascade removed the chunks. Fixed by preferring recomputed
  `active_date_min/max`.
- **First quarantine bar was far too lenient** — the `AND
  structurally_broken` condition meant only 3 of ~26k chunks
  quarantined, so RAG kept skimming unreadable pages. Rebuilt as the
  two-stage bar above, plus the cluster-refusal corrective.
- **Order-of-operations bug in the corrective** — we ran the
  cluster-refusal quarantine using labels generated *before* the
  per-chunk quarantine, so a cluster's whole membership (750 chunks)
  got swept in on stale evidence. A `--dry-run`-then-real revert showed
  **61% (459/750) were individually readable** and wrongly quarantined.
  Now: per-chunk pass → revert-with-per-chunk-verdict → recompute →
  relabel → corrective, so Haiku judges the *cleaned* cluster. (A
  case-sensitive `"True" != "true"` bug in the revert workflow input
  briefly ran it non-dry; fixed with a boolean input + lowercasing.)
- **Quarantine Recovery System (targeting) — built, diagnostic,
  shelved.** The idea: score which quarantined ("dark") pages a
  historian should eyeball for a research topic, using free signals —
  a gazetteer from active text, surviving legible fragments, trigram +
  OCR-damage-variant entity matching, a layout-slot grid, cluster
  footprints + gap detection, and quality-weighted embedding
  proximity, combined into a composite `recovery_value`. Phase A is
  built (migration 0010, `scripts/recovery_score.py`) and produced
  genuinely useful diagnostics. Across several tuning rounds we fixed
  real defects (common-surname entity noise, a signed commercial-vs-
  editorial grid term, a gap-bonus that amplified commercial noise,
  keyword word-boundaries). **But the whole effort was then invalidated
  by an answer-key experiment — see the postmortem below.** The tables
  and scripts remain in the repo as a foundation if the premise ever
  holds for a different corpus; nothing is wired to a live surface.

---

## Postmortem — the American Stories answer-key experiment

**Question we set out to answer:** can we tune the recovery-targeting
heuristic to reliably surface quarantined pages relevant to a research
theme? We couldn't measure this directly (the chunks are unreadable),
so we used Harvard's **American Stories** dataset — clean, layout-aware
OCR of the *same* Chronicling America pages — as an answer key.

**Method (three gated phases, all read-only or $0):**
- **Gate 1** confirmed both our papers are in American Stories 1845
  (Tribune 1,249 records, Herald 1,486 — effectively full-year).
- **Gate 2** joined 1,382 quarantined chunks to their clean AS page/
  region text, labeled each for the test theme "political violence"
  (lexicon + a 100-call Haiku cap), split tune 70 / holdout 30, and
  baselined the current heuristic: **precision@15 = 0.07** on both
  splits.
- **Phase 3** ran a pre-registered 19-candidate tuning loop (word
  boundaries, distinctive-entity gating, phrase-level lexicon, grid
  softening, density penalties, weight sweeps), accept-on-tune-
  improvement with holdout overfit checks. **Zero candidates improved
  P@15.** The four "fix the diagnosed bug" candidates each crashed it
  to 0.000 individually — the one true positive in the top-15 was
  propped up by the *same* defective signals as the false positives,
  so any single fix dropped it too (a defect-supported local optimum).

**The finding that ended the plan — there was nothing to find.** An
oracle ranker with perfect lexicon knowledge scored **P@15 = 0.00**.
Of 59 tune positives, only 1–3 retained *any* fuzzy-recoverable theme
token in their garbled text — while negatives hit *more* often (8–16%,
from incidental "murder"/"county" noise). Digging into the labels:
the top driver term was "outrage" (47/85 — a lexicon mistake; generic
1845 indignation vocabulary), 50/59 tune positives were page-level
labels bleeding from unrelated articles on the same page, and the
handful of genuine region-aligned political-violence pages spanned
just ~24 distinct pages. Crucially, **American Stories is itself
degraded on exactly these pages** — Harvard OCR'd the same bad film —
so the "answer key" is blurry precisely where we needed it.

**Conclusion.** The premise was false for *this* corpus: the dark pool
is overwhelmingly Herald page-4 shipping/commercial matter on damaged
reels, and the political-violence coverage largely *survived*
digitization as active chunks (which is why RAG already finds it).
There was never a top-15 of hidden relevant pages to rank. This is the
methodology working, not failing — two gates and one $0 tuning run
told us the heuristic can't be tuned to find what isn't there, and
surfaced real structural bugs in the composite worth fixing on
engineering grounds regardless. The recovery system is shelved as
described above.

**Reusable by-products:** the frozen labeled eval set lives at
`data/recovery_eval/`; the gate/tuning scripts
(`scripts/american_stories_gate1.py`, `_gate2.py`,
`scripts/recovery_tuning.py`) are a working template for
answer-key-driven heuristic evaluation on any corpus that has a
cleaner twin.

---

## What's next

The active candidates for the next increment:

1. **Herald-as-engine extraction** — factor the reusable core
   (ingest → chunk → embed → cluster → drift/geometry → brief →
   dossier) out of the newspaper-specific corpus so it can be pointed
   at other document sets. First target: Westchester County public-
   school governance (board minutes, policy docs) across districts
   over time. Scoping in progress.
2. **Pool-composition characterization** (cheap salvage from the
   recovery work) — run the clean American Stories text over the whole
   quarantined pool to quantify what it actually contains (% shipping /
   ads / news-like), to power honest dossier "dark matter" copy and
   the cross-paper divergence caveats.
3. **LoC unblock follow-up** — if access is restored, resume corpus
   expansion and revisit vision re-OCR (now understood as a targeted,
   paid step that should only fire on pre-qualified pages).
4. **Broader-tier label quality** — tier 2/3 labels remain noisier
   than tier 0/1.
