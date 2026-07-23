# Topic map (cluster scatter)

The first visualization surface: every chunk as a dot, laid out by semantic
similarity, colored by discovered topic or by district. Answers "what themes
are in the corpus, and which districts sit where."

## Pipeline

```
content embed ─► UMAP (→10-D) ─► HDBSCAN ─► leaf topics ─┐
                                    │                     ├─► chunk JSON ─► cluster_map.html
                    Ward-merge centroids → theme tiers    │   (artifact)   (canvas point cloud,
                                 Haiku labels (all tiers) ─┤                 recolor by level
        project *every chunk* → 2-D (cosine, direct) ─────┘                 + drill-down tree)
```

- **`herald.cluster_schools`** loads active chunks, **UMAP-reduces to a
  mid dimensionality (`cluster_dims`, default 10)**, clusters that with
  HDBSCAN (leaf method, `-1` noise preserved), **projects every chunk to 2D**
  with a direct cosine UMAP for the map, picks the chunks nearest each cluster's
  embedding centroid as representatives, and labels each topic with Haiku.
  Export-only (writes no cluster tables — the map reads a JSON artifact).

  **Learnings from the first real runs** (baked into the defaults):
  - *Cluster in ~10-D, not 1024-D and not 2-D.* Raw 1024-D drowns ~60% of
    points in the noise bin (distance concentration); straight-to-2D
    over-merges (the 2D layout optimizes visual separation, not cluster
    density). A mid-dimensional cosine UMAP (`min_dist=0`) is the standard
    middle ground for the *clustering* space.
  - *Project chunks directly, with cosine.* The map is **one dot per chunk**
    (like the inherited newspaper map). The projection is a **cosine** UMAP
    straight on the embeddings (`project_chunks`) — that preserves semantic
    locality, so a topic's passages co-locate into a visible cluster *and* a
    keyword's matches sit together even across cluster lines. An earlier attempt
    projected the *already-reduced 10-D* space with a euclidean metric; that
    chaining destroyed locality and scattered a single topic across the plane.
    (Aggregating to one bubble per topic hid that scatter but also threw away
    the point-level locality that makes search-on-the-map work.)
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
- **Output** is columnar per-chunk JSON: parallel arrays `x`, `y`, `cluster`
  (leaf id), `district`, `month`, one entry per chunk (~a few hundred KB — plain
  numbers compress well). `clusters` lists the leaf topics
  `{id, label, size, theme, mid, tip}` (`theme`/`mid` are the hierarchy parents,
  `tip` a representative snippet); `hierarchy` carries the coarsest-first tiers
  of `{id, label, size, leaves}`. The renderer recolors the *same* points by
  theme / topic / district and drives the legend from `clusters` + `hierarchy`.

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

**The map**: **one dot per chunk**, pan / zoom / pinch. **Color by** recolors
the *same* points three ways — **Theme** (~15 hues → clear regions), **Topic**
(the fine leaf clusters), or **District**. The legend is a **drill-down tree** —
broad themes expand to topics expand to leaf topics (from the export's
`hierarchy`); tapping a branch isolates its points *and frames them* (the map
pans/zooms to the selection), so you narrow from "Personnel & labor" to
"Substitute & coaching stipends" in two taps. **Search** highlights every point
whose topic label matches and dims the rest — because the projection preserves
locality, the matches show up as a tight region (the "426 Oregon matches land
together" behavior). Also: filter by district chips, tap a dot for its topic ·
district · date · snippet. Dark and light themes; mobile-first (the control rail
becomes a bottom sheet).

## Design notes

- **Theme colors** are evenly spaced around the HSL wheel (themes are few — ~15
  — so they stay far apart); **Topic colors** use a golden-angle rotation (many
  leaves, so spatial position disambiguates); **District colors** a fixed 8-hue
  set. Noise (`-1`) is a neutral gray.
- Rendering is Canvas 2D with color-batched draw (one `fillStyle` per color
  group). When a focus is active (isolation ∩ search), a faint context pass
  draws the rest at low alpha so you keep the shape of the whole corpus — this
  handles ~23k points at interactive frame rates without WebGL.
- Verified headless (Playwright + the pre-installed Chromium) across
  desktop/mobile and both themes before shipping.

## Next views (build on this)

The per-chunk `district` / `month` arrays and the hierarchy in the export
already support the other planned views without re-clustering: district
comparison, single-topic dossier, and topic-over-time (trajectory). This map is
their shared substrate.
