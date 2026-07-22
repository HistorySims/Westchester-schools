# Topic map (cluster scatter)

The first visualization surface: every chunk as a dot, laid out by semantic
similarity, colored by discovered topic or by district. Answers "what themes
are in the corpus, and which districts sit where."

## Pipeline

```
chunks.embedding  ──►  UMAP (2D)          ─┐
                  ──►  HDBSCAN (topics)     ├─► compact JSON ─► cluster_map.html
                  ──►  Haiku (topic labels)─┘        (artifact)     (canvas scatter)
```

- **`herald.cluster_schools`** loads active chunks, **UMAP-reduces to a
  mid dimensionality (`cluster_dims`, default 10)**, clusters that with
  HDBSCAN (leaf method, `-1` noise preserved), projects the *same reduced
  space* to 2D for display, picks the chunks nearest each cluster's embedding
  centroid as representatives, and labels each topic with Haiku. Export-only
  (writes no cluster tables — the map reads a JSON artifact, not the DB).

  **Learnings from the first real run** (baked into the defaults):
  - *Cluster in ~10-D, not 1024-D and not 2-D.* Raw 1024-D drowns ~60% of
    points in the noise bin (distance concentration); straight-to-2D
    over-merges (the 2D layout optimizes visual separation, not cluster
    density). A mid-dimensional cosine UMAP (`min_dist=0`) is the standard
    middle ground. The 2D display is a projection *of* that reduced space,
    so the colored regions correspond to the clusters.
  - *Cluster on **content-only** embeddings.* The stored `chunks.embedding`
    carries the contextual prefix (`"{district} · {date} · …"`) — right for
    retrieval, but it makes the map cluster by *district* (the first run's
    top clusters were ~100% single-district). `--embeddings content`
    re-embeds each chunk's raw content with Voyage (~$0.35 for the corpus,
    needs `VOYAGE_API_KEY`) so the map organizes by topic. `--embeddings
    stored` reuses the district-prefixed vectors (a *district* map).
- **Output** is deliberately *columnar* JSON (parallel arrays, not per-point
  objects) to keep ~23k points small: `x`, `y`, `cluster`, `district`,
  `doc_type`, `month`, `tip`, plus a `clusters` list of `{id, label, size}`
  and the district / doc-type index tables. A full run is ~2 MB.

## Running it

Workflow **`cluster`** (Actions → cluster → Run workflow):

| input | meaning |
|---|---|
| `sample` | random-sample N chunks for a lighter map (empty = all) |
| `min_cluster_size` | HDBSCAN granularity — larger = fewer, broader topics |
| `embeddings` | `content` (topic map) or `stored` (district map) |
| `label` | label topics with Haiku (needs `ANTHROPIC_API_KEY`) |

It uploads `cluster-map.json` as an artifact and prints the topic table to the
run summary. Needs `SUPABASE_DB_URL`, `VOYAGE_API_KEY` (for `--embeddings
content`), and `ANTHROPIC_API_KEY` (for labels). CLI equivalent:
`herald-cluster run --out cluster-map.json [--sample N] [--min-cluster-size 15]
[--embeddings content|stored] [--labels/--no-labels]`. Content re-embedding is
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

**The map**: canvas scatter, pan / zoom / pinch, tap the legend to isolate a
topic, filter by district chips, toggle color between topic and district,
hover or tap a point for its district · date · doc-type · snippet. Dark and
light themes; mobile-first (the control rail becomes a bottom sheet).

## Design notes

- **Topic colors** use a golden-angle HSL rotation so adjacent topics
  separate; there are more topics than any categorical palette can hold, so
  spatial position disambiguates. **District colors** use a fixed 8-hue set.
- Rendering is Canvas 2D with color-batched draw (one `fillStyle` per color
  group), which handles ~23k points at interactive frame rates without WebGL.
- Verified headless (Playwright + the pre-installed Chromium) across
  desktop/mobile and both themes before shipping.

## Next views (build on this)

The `clusters` + per-point `district`/`month` in the export already support
the other three planned views without re-clustering: topic-over-time
(trajectory), district comparison, single-topic dossier. See the options in
the project history; this map is their shared substrate.
