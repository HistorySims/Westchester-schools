# Topic map (cluster scatter)

The first visualization surface: every chunk as a dot, laid out by semantic
similarity, colored by discovered topic or by district. Answers "what themes
are in the corpus, and which districts sit where."

## Pipeline

```
content embed ─► UMAP (→10-D) ─► HDBSCAN ─► leaf topics ─┐
                                    │                     ├─► topic JSON ─► cluster_map.html
                    Ward-merge centroids → theme tiers    │    (artifact)   (canvas bubble map
                                 Haiku labels (all tiers) ─┤                  + drill-down tree)
        project *topic centroids* → 2-D (cosine) bubbles ─┘
```

- **`herald.cluster_schools`** loads active chunks, **UMAP-reduces to a
  mid dimensionality (`cluster_dims`, default 10)**, clusters that with
  HDBSCAN (leaf method, `-1` noise preserved), **projects the topic centroids
  to 2D** for the bubble layout, picks the chunks nearest each cluster's
  embedding centroid as representatives, and labels each topic with Haiku.
  Export-only (writes no cluster tables — the map reads a JSON artifact).

  **Learnings from the first real runs** (baked into the defaults):
  - *Cluster in ~10-D, not 1024-D and not 2-D.* Raw 1024-D drowns ~60% of
    points in the noise bin (distance concentration); straight-to-2D
    over-merges (the 2D layout optimizes visual separation, not cluster
    density). A mid-dimensional cosine UMAP (`min_dist=0`) is the standard
    middle ground for the *clustering* space.
  - *Lay out **topics**, not chunks.* The map is a **bubble map** — one disk
    per topic at its centroid, radius ∝ passage count — so it projects the 539
    topic centroids to 2D with a **cosine** UMAP (`project_topics`). An earlier
    version projected all 23k chunks (and via a euclidean projection of the
    already-reduced space): a single topic's passages scattered to opposite
    ends of the plane and the whole thing read as confetti. Projecting the
    centroids with cosine keeps each topic one tight bubble and related topics
    near each other.
  - *Cluster on **content-only** embeddings.* The stored `chunks.embedding`
    carries the contextual prefix (`"{district} · {date} · …"`) — right for
    retrieval, but it makes the map cluster by *district* (the first run's
    top clusters were ~100% single-district). `--embeddings content`
    re-embeds each chunk's raw content with Voyage (~$0.35 for the corpus,
    needs `VOYAGE_API_KEY`) so the map organizes by topic. `--embeddings
    stored` reuses the district-prefixed vectors (a *district* map).
- **Hierarchy (cluster-of-clusters).** A single flat granularity can't be both
  clean and legible — the sweep showed fine clustering (`min_cluster_size=15`)
  gives the best DBCV and lowest noise but ~200 topics on a sample (~500 on the
  full corpus), while coarse clustering is legible but noisier. So we cluster
  *fine* for clean leaves, then **merge the leaf centroids** upward
  (`herald.cluster_schools.build_hierarchy`: one **Ward** agglomerative linkage,
  cut at each `--tiers` count, default `15,60`). Ward matters: `average`/
  `complete` linkage chains in high-D — it peels outlier leaves off as
  singleton "themes" while dumping the mass into a few 200-leaf blobs (the
  first full run did exactly this); Ward minimizes within-tier variance for
  balanced, browsable themes. Leaf centroids are L2-normalized so Ward's
  Euclidean metric clusters in cosine space. Because every tier is a cut of the
  *same* linkage, the tiers **strictly nest** — a leaf's ancestors are
  well-defined (unlike re-running HDBSCAN per level, whose partitions needn't
  agree). Each tier is labelled by Haiku from passages pooled across its child
  leaves. This is why the merge beats independent HDBSCAN runs at 15/30/60:
  nesting is guaranteed by construction and it costs one small linkage, not
  three full re-clusters.
- **Output** is a compact *per-topic* JSON — the 23k chunks are aggregated
  away, so it's small (a few hundred KB, not the 3.6 MB the old per-chunk
  export was). Each leaf topic in `clusters` is a bubble:
  `{id, label, size, x, y, theme, mid, dist, tip}` — `x`/`y` its 2D position,
  `theme`/`mid` its hierarchy parents, `dist` a per-district passage histogram
  (for district coloring / filtering), `tip` a representative snippet. The
  `hierarchy` carries the coarsest-first tiers of
  `{id, label, size, x, y, leaves}`, plus the district / doc-type index tables.

## Running it

Workflow **`cluster`** (Actions → cluster → Run workflow):

| input | meaning |
|---|---|
| `sample` | random-sample N chunks for a lighter map (empty = all) |
| `min_cluster_size` | HDBSCAN granularity — larger = fewer, broader topics |
| `tiers` | coarse hierarchy tiers merged from leaf centroids (broadest first; empty = flat) |
| `embeddings` | `content` (topic map) or `stored` (district map) |
| `label` | label topics with Haiku (needs `ANTHROPIC_API_KEY`) |

It uploads `cluster-map.json` as an artifact and prints the theme + topic
tables to the run summary. Needs `SUPABASE_DB_URL`, `VOYAGE_API_KEY` (for
`--embeddings content`), and `ANTHROPIC_API_KEY` (for labels). CLI equivalent:
`herald-cluster run --out cluster-map.json [--sample N] [--min-cluster-size 15]
[--tiers 15,60] [--embeddings content|stored] [--labels/--no-labels]`. Content re-embedding is
recomputed each run; if we settle on it we'll store a `content_embedding`
column to avoid the repeat cost.

## Tuning it (the sweep)

Before committing to a granularity, run the **`cluster-sweep`** workflow
(Actions → cluster-sweep → Run workflow). It grid-searches
`cluster_dims` × `min_cluster_size`: for each dimension it UMAP-reduces once
(the expensive step) then re-runs HDBSCAN across every `min_cluster_size`,
reporting **topic count**, **noise %**, and **DBCV** (density-based cluster
validity — HDBSCAN's `relative_validity_`, higher = cleaner separation). It
runs on a `sample` (default 8k chunks — the *relative* ranking holds on a
sample, and it's faster/cheaper). Defaults: `dims=5,10,15,20`,
`min_cluster_sizes=15,30,60,100`. Output is a markdown table (`cluster-sweep.md`,
printed to the run summary, ranked by DBCV). Pick the cell that trades off
clean separation against a legible topic count, then feed those numbers into
the `cluster` workflow. CLI: `herald-cluster sweep [--sample N] [--dims …]
[--min-cluster-sizes …] [--embeddings content|stored]`.

**On hierarchy** (asked during design): for nested topics — broad themes that
split into sub-topics — prefer **cluster-of-clusters** (agglomerative merge on
cluster centroids) over re-running HDBSCAN at 15/30/60. Separate HDBSCAN runs
give three *independent* partitions that need not nest (a point can land in
different parents at different granularities); merging centroids upward
*guarantees* nesting and costs one small linkage, not three full re-clusters.

## Viewing it

`viz/cluster_map.html` is a self-contained renderer (no external libraries —
CSP-safe). It reads the JSON from its `#map-data` script tag; the
`__MAP_DATA__` token is replaced with the exported JSON to produce a viewable
page. To get it on a phone: run the `cluster` workflow, share the
`cluster-map.json` back, and it's published as a private Artifact link.

**The map**: a **bubble map** — one disk per topic, radius ∝ passage count,
colored by theme. Pan / zoom / pinch. The legend is a **drill-down tree** —
broad themes expand to topics expand to leaf topics (from the export's
`hierarchy`); tapping a branch opens it, isolates its bubbles, *and frames them*
(the map pans/zooms to the selection), so you narrow from "Personnel & labor"
down to "Substitute & coaching stipends" in two taps. Labels appear only when a
small set is isolated or you zoom in (greedy collision-avoidance, so they never
pile up). Searching flattens the tree to matching leaf topics. Also: filter by
district chips, toggle color between **theme** and **district** (dominant
district per bubble — "which districts sit where"), hover or tap a bubble for
its label · size · top districts · snippet. Dark and light themes; mobile-first
(the control rail becomes a bottom sheet).

## Design notes

- **Theme colors** are evenly spaced around the HSL wheel (themes are few — ~15
  — so they stay far apart); a leaf bubble takes its theme's hue, so themes read
  as colored regions. **District colors** use a fixed 8-hue set.
- Rendering is Canvas 2D. There are only ~hundreds of topic bubbles (not 23k
  chunks), so each is a plain `arc` fill drawn largest-first (small bubbles stay
  clickable on top); interactive without WebGL.
- Verified headless (Playwright + the pre-installed Chromium) across
  desktop/mobile and both themes before shipping.

## Next views (build on this)

Each topic's `dist` histogram and hierarchy parents in the export already
support the other planned views without re-clustering: district comparison
(from `dist`), single-topic dossier, and — once we add a per-topic month
histogram — topic-over-time. This bubble map is their shared substrate.
