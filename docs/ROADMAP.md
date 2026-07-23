# Westchester Schools — Roadmap

Where the project is headed after the batch pipeline settled: a hosted web
app, and — the genuinely new problem the 1840s corpus never had — **the corpus
keeps growing**. New board meetings happen every month. This doc captures the
plan and the design decisions behind it, so it's the reference when we start
building.

See [`STATUS.md`](STATUS.md) for what's done today, [`VIZ.md`](VIZ.md) for the
map, [`ASK.md`](ASK.md) for the ask layer.

---

## 1. Move to Vercel

Today everything is GitHub Actions batch jobs that emit artifacts (JSON maps,
answers) we shuttle to the phone. That was the right bootstrap, but it's the
friction we keep paying. A hosted app removes it.

**What moves:**
- **Vercel app reads Supabase** for the map, the ask box, and the monthly
  brief. Server-side queries mean **full-text search is free** — the whole
  "ship a search index inside the artifact" problem simply evaporates; search
  becomes a Postgres `ILIKE` / full-text query returning matching chunk ids.
  No artifact size ceiling.

**What stays in GitHub Actions:**
- All the heavy batch work — crawl, ingest, embed, cluster, assign, drift,
  brief. These stay scheduled/`workflow_dispatch` workflows writing to
  Supabase. Vercel never does the compute; it reads the results.

So the split is clean: **Actions writes, Vercel reads, Supabase is the seam.**

---

## 2. The monthly cycle

New board packets arrive monthly. The naive move — re-run UMAP + HDBSCAN on the
whole corpus every month — is **wrong**, because it renumbers every topic each
run: "topic 47" this month isn't "topic 47" next month, colors and positions
shuffle, and you can't say "special-ed discussion grew" because the topics have
no stable identity across runs. A monthly brief *needs* stable topics to talk
about.

So the pattern is **freeze-and-assign**, re-clustering only occasionally.

**Monthly (cheap, stable) — a scheduled workflow:**
1. Crawl + ingest the new packets → embed content-only.
2. **Assign** each new chunk to the nearest existing topic by cosine similarity
   to that topic's stored centroid. No new clustering. (We already store a
   `centroid` per cluster, like the inherited `clusters` table.)
3. Chunks far from *every* centroid are flagged **emerging-topic candidates**,
   not force-fit.
4. Compute drift (see §4) and generate the brief.
5. Write to Supabase; Vercel surfaces it.

Assignment needs only the stored centroids and a cosine compare — **no pickled
UMAP/HDBSCAN models to persist.** New points get a map position near their
assigned topic's 2D centroid; true positions are recomputed at the next full
re-cluster.

---

## 3. Re-cluster policy

Full re-cluster = relearn UMAP + HDBSCAN + Ward hierarchy + labels from scratch.
It happens **on a half-year floor, plus a rare signal trigger**:

- **Floor:** every 6 months regardless.
- **Signal:** sooner *only* when the frozen model has clearly outgrown reality
  — emerging-topic candidates piling up, and/or a topic cleanly **splitting**
  (see turf-vs-grass in §4). Tuned to fire rarely.

**Continuity across a re-cluster:** when we do relearn, match new clusters to
old ones by **centroid similarity** and carry forward ids / labels / colors
where they match, so trends survive the re-cluster and the map doesn't reshuffle
under the user. This is what the inherited `cluster_drift` machinery is for.

---

## 4. The monthly brief — drift signals

The brief answers *"what's different, and who's doing something?"* across the
districts. Two design rules shape it:

**Rule 1 — compare to the same period in prior years, not last month.** School
years are cyclical: month-over-month would flag "prom!" every May and "budget!"
every spring. The baseline is **this period vs the distribution of all prior
same-periods** (a seasonal z-score). A topic only flags when this year's slice
is unusual *against its own seasonal norm* — turning "prom is being discussed"
(noise) into "prom discussion is abnormal this May" (signal).

**Rule 2 — the signal is *change*, not *volume*.** "Who's making a major change
to their prom system" isn't a volume spike; it's **novel content inside an
expected topic**. So detection is two layers: a cheap volume layer, and a
content layer (chunks semantically far from a topic's historical center, or
carrying change-language — "adopted", "discontinued", "replaced", "pilot").
The content layer is the "needs our engine, not just RAG" work: retrieval + a
judged pass.

### Signal menu

| Signal | What it is | Difficulty |
|---|---|---|
| **Seasonal drift** | topic / district unusual vs the same period in prior years | easy (volume) |
| **Cross-district divergence** | one district present or absent where its peers aren't | easy (per-district shares) |
| **Emerging topics** | far-from-every-centroid chunks piling up | easy (also the re-cluster signal) |
| **Emerging outliers** | the "coach paid abnormally" flavor, but newly appearing | medium (retrieval + judge) |
| **Structural change in a topic** | "district X overhauled its prom system" | medium (content-novelty layer) |
| **Splits / controversies** | "athletics fields" bifurcating into turf vs grass | medium (per-topic 2-way split) |
| **Storylines** | narrative threads over time | **deferred** — see §6 |

**Splits are dual-purpose.** Periodically test each topic for bimodality (a
clean, growing, stance-opposed 2-way split). A topic that's cleanly splitting is
both a **brief-worthy controversy** *and* the clearest **re-cluster signal** —
it's the frozen model going stale in an interesting way. Cheap: run per-topic,
only on topics with enough recent volume.

This is largely the inherited `cluster → drift → brief` engine
(`db/newspaper/0007_cluster_drift.sql`, `0005_cluster_stories.sql`,
`0009_cluster_weeks.sql` — the "Bursty Topics" panel), ported to the schools
schema and fed **seasonal (YoY)** windows instead of a historical timeline.

---

## 5. Data prerequisite

Year-over-year needs **≥1 year of history assigned to the current topics.** We
get it for free: assignment is retrospective, so we back-assign the whole
BoardDocs archive to the current model and the seasonal baselines come from the
backfill. **Caveat to confirm:** some districts may only have 2024→now of
archive; their YoY baselines are thin until we accumulate more cycles. Districts
with deep archives get trustworthy seasonal norms immediately.

---

## 6. Deferred: storylines

The big one — following a *narrative thread* over time ("the turf-field fight,
from first proposal to vote") — is deferred until after **trajectory analysis**
(topic-over-time), which is the substrate that makes a "storyline" definable.
Trajectory is the next visualization after the topic map; storylines build on
it. Likely its own project.

---

## Sequencing

1. **Full-text search** — either in the current artifact (client-side index,
   ~5 MB) or, preferably, wait for Vercel where it's a free server-side query.
2. **Trajectory view** (topic-over-time) — next visualization; substrate for
   both the seasonal brief and storylines.
3. **Vercel app** — map + ask + brief reading from Supabase.
4. **Monthly cycle** — schema (`centroid` column, schools-schema drift tables),
   the assign workflow, the seasonal drift + brief generator, scheduled monthly.
5. **Storylines** — after trajectory lands.
