# Forking the Herald engine to a new corpus

How to take Herald and stand up a **clean fork** for a different
document set — the first target being Westchester County public-school
governance (board minutes, policy docs) across districts over time.

Decision recorded (July 2026): **clean fork, diverge freely.** The two
repos share no code after the split. This is the right call for a solo
maintainer building a distinct product; the shared-engine overhead
only pays off with a team. The cost is that engine fixes won't flow
between the repos automatically — accept that and move on.

First milestone: **cross-district on one narrow policy topic** — ingest
a few districts but only documents touching a single policy area, to
prove the cross-district comparison before scaling.

---

## The mental model: engine vs. corpus

Herald is really two things wired together:

- **The engine** — chunk → embed → cluster → drift/geometry → hybrid
  retrieval → Brief → Dossier. This is domain-agnostic and is the part
  that "works very well." It transfers almost unchanged.
- **The corpus adapter** — everything that knows the documents are
  1840s newspapers from Chronicling America: the LoC client, the
  `papers/issues/pages` schema, the "rewrite into 1840s diction"
  prompt, the per-*paper* axis, the OCR-quality/quarantine machinery,
  the LoC page-image viewer.

Forking = keep the engine, replace the adapter.

---

## What a "corpus adapter" has to supply

Four seams. Everything domain-specific lives in exactly these:

1. **Schema + ingest** — what a source, a document, and a date *are*,
   and how raw files become `chunks`. Newspaper: `papers → issues →
   pages → chunks`, fed by the loc.gov API. Schools: `districts →
   documents → chunks`, fed by PDF/transcript parsing. (A board-meeting
   PDF collapses newspaper "issue + page" into one "document" with a
   date and a type.)
2. **The cross-source axis** — newspaper uses "paper" as the dimension
   that varies (the Dossier streamgraph is per-paper). Schools uses
   "district." Same mechanism, relabel throughout.
3. **The time model** — newspapers are daily; the weekly bucketing in
   drift + `cluster_weeks` assumes dense dates. Board minutes are every
   few weeks and organized by **school year**. Likely change: weekly →
   monthly buckets, and a school-year grouping alongside calendar date.
4. **Domain vocabulary in prompts** — the Brief's translation pass
   ("rewrite into 1840s newspaper diction") becomes "rewrite into the
   jargon of school governance" (BOE, superintendent, Regents, IEP,
   Title I, bond referendum…). The synthesis prompt's paper-attribution
   language becomes district-attribution. Same prompt *structure*.

A happy side effect: **the OCR-quarantine pain mostly evaporates.**
Board minutes are born-digital PDFs or clean scans, so the whole
`quality/quarantine/recovery` apparatus that gave us the most grief
is optional-to-irrelevant. Don't port it.

---

## File-by-file manifest

Accurate to the repo as of this writing. Three buckets.

### KEEP — copy as-is (only the package rename touches them)

The domain-agnostic engine.

**Python (`src/herald/` → `src/<newpkg>/`)**
`chunker.py`, `cluster.py`, `db.py`, `embed.py`, `rerank.py`,
`retrieval.py`, `settings.py`, `normalize.py`, `quality.py`,
`wordlist.txt`, `__init__.py`, `__main__.py`

**Web lib (`web/src/lib/`)**
`brief.ts`, `burstiness.ts`, `dossier.ts`, `explore-data.ts`,
`rate-limit.ts`, `retrieval.ts`, `supabase.ts`, `voyage.ts`

**Web components (`web/src/components/`)**
`BurstyTopics`, `ChatPane`, `ChunkDetail`, `CitationLink`,
`ClusterAnatomy`, `ClusterStory`, `EvidenceFeed`, `ExploreMap`,
`ExploreSidebar`, `FilterControls`, `MessageBubble`, `ResearchBrief`,
`SearchBox`, `TimelineMinimap`
*(all carry "paper" labels in copy — relabel to "district" as you go,
but the logic is unchanged)*

**Migrations** `0002`–`0009` (clustering, RPCs, labels, stories, drift,
active geometry, cluster_weeks) — reference the schema, so they need
the same `papers→districts / issues→documents` rename as `0001`, but no
structural change.

**Scripts** `cluster_drift.py`, `cluster_recompute.py`,
`cluster_weeks.py`, `export_cluster_labels.py`, `score_chunk_quality.py`

**Workflows** `ci.yml`, `cluster.yml`, `cluster-drift.yml`,
`cluster-recompute.yml`, `cluster-labels.yml`, `relabel.yml`,
`score-quality.yml`

### REWRITE — structure transfers, domain content changes

- `src/herald/models.py` — the pydantic schema. `Paper→District`,
  `Issue→Document` (fold `Page` in, or keep pages if your PDFs are
  long), `Chunk` unchanged. **Decision point:** collapse issue+page or
  keep both. For minutes (one PDF per meeting) collapse; for long
  policy manuals you may want pages.
- `src/herald/ingest.py` — the orchestrator. Its *shape* (fetch →
  normalize → chunk → embed → write) stays; the fetch source swaps from
  loc.gov to your PDF/transcript parser. **This is the milestone-1
  build and it's blocked on knowing the data shape** (you chose "mixed
  / not sure yet") — stub it, fill it once you have sample files.
- `src/herald/cli.py` — commands. `ingest --lccn/--from/--to` becomes
  `ingest --district/--year` or similar.
- `src/herald/classify.py` — keep the quality sub-scores; **drop** the
  ad/legal/bad-OCR newspaper content-type classifier (irrelevant to
  minutes).
- `src/herald/synth.py` and `web/src/lib/synth.ts` — the prompts.
  Rewrite the persona/attribution language for districts; keep the
  citation discipline and structure verbatim.
- `web/src/lib/types.ts` — `paper_title/paper_lccn` → `source/district`.
- `web/src/components/PageViewer.tsx` — the LoC image card → a PDF-page
  link/embed for your source documents.
- Web routes (`web/src/app/{page,brief,cluster,explore}` + `api/*`) —
  relabel "paper"→"district" in copy and params; logic unchanged.
- Migrations `0001_init.sql` and `0003_explore_rpcs.sql` — the schema.
  Rename tables/columns per the models decision above.
- `ingest.yml` — inputs change from `lccn/date` to `district/year`.

### STRIP — delete entirely

Newspaper-specific or the shelved recovery work.

- **Python** `loc.py` (Chronicling America client)
- **Migration** `0010_quarantine_recovery.sql`
- **Scripts** `american_stories_gate1.py`, `american_stories_gate2.py`,
  `recovery_score.py`, `recovery_tuning.py`,
  `quarantine_by_cluster_refusal.py`, `quarantine_probe.py`,
  `revert_cluster_refused.py`, `bertopic_diagnostic.py`
- **Workflows** `answer-key-gate1.yml`, `answer-key-gate2.yml`,
  `recovery-score.yml`, `recovery-tuning.yml`,
  `quarantine-cluster-refusal.yml`, `quarantine-probe.yml`,
  `revert-cluster-refused.yml`, `bertopic.yml`
- **Data** `data/recovery_eval/` (the frozen answer-key eval set)
- **Docs** the newspaper STATUS/PLAN docs (write a fresh STATUS for the
  new product)

`src/herald/eval.py` — review before deciding; it's a retrieval-eval
harness, keep it if you want offline retrieval metrics.

---

## Mechanical steps to birth the new repo

`scripts/bootstrap_fork.sh` automates the safe parts (delete the STRIP
list, rename the Python package, drop TODO banners on the REWRITE
files). It does **not** auto-rewrite logic — you review each REWRITE
file yourself.

```bash
# 1. Clone Herald into a new working directory (no shared history)
git clone https://github.com/HistorySims/herald.git schools-engine
cd schools-engine
rm -rf .git                      # fresh history for a distinct product

# 2. Run the bootstrap: strips newspaper/recovery files, renames the
#    package herald -> <newpkg>, banners the REWRITE files.
bash scripts/bootstrap_fork.sh schoolsengine "Westchester schools governance research"

# 3. Start fresh git history and create the GitHub repo (your account)
git init && git add -A && git commit -m "Initial fork of the Herald engine"
gh repo create <you>/schools-engine --private --source=. --push
#    (or create the repo in the GitHub UI and `git remote add` + push)

# 4. Provision a NEW, SEPARATE Supabase project. Do not reuse Herald's
#    database. Set SUPABASE_DB_URL / SUPABASE_URL / SUPABASE_SERVICE_KEY
#    / VOYAGE_API_KEY / ANTHROPIC_API_KEY as repo Actions secrets, same
#    names as Herald.
```

After that you're in the REWRITE list: schema first (models + 0001 +
0003), then the ingest adapter once you have sample documents, then a
pass over prompts and UI copy.

---

## Suggested build order for milestone 1

1. **Schema** — decide the district/document/chunk shape, rewrite
   `models.py` + `0001` + `0003`, apply to the new Supabase project.
2. **Ingest adapter** — once you have 5–10 sample PDFs from 2–3
   districts on your chosen policy topic: write the parser (PDF text →
   normalized text → chunks), wire it into `ingest.py`, embed, write.
   Start tiny; correctness over coverage.
3. **Cluster + geometry** — run the existing `cluster` +
   `cluster-recompute` + `cluster-weeks` workflows unchanged. If the
   dates are sparse, switch the weekly bucket to monthly first.
4. **Prompts** — rewrite the translation + synthesis prompts for
   school-governance vocabulary.
5. **Brief + Dossier** — should work with only copy relabeling
   (paper→district). This is the proof: ask a cross-district policy
   question, get a finding aid.

Don't build the ingest adapter until you have real sample files in
hand — the "mixed / not sure yet" data shape is the one thing that can
invalidate an adapter written on assumptions.
