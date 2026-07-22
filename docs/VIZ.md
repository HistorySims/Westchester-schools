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

- **`herald.cluster_schools`** loads active, embedded chunks from the schools
  schema, projects them to 2D with UMAP (cosine, normalized to `[0,1]`),
  clusters with HDBSCAN (leaf method, `-1` noise preserved), picks the chunks
  nearest each cluster centroid as representatives, and labels each topic with
  Haiku. Reuses the UMAP/HDBSCAN parameters from the inherited newspaper
  `cluster.py`; this is the schools-schema, export-only cut (writes no cluster
  tables — the map reads a JSON artifact, not the DB).
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
| `label` | label topics with Haiku (needs `ANTHROPIC_API_KEY`) |

It uploads `cluster-map.json` as an artifact and prints the topic table to the
run summary. Needs `SUPABASE_DB_URL` (and `ANTHROPIC_API_KEY` for labels).
CLI equivalent: `herald-cluster run --out cluster-map.json [--sample N]
[--min-cluster-size 15] [--no-labels]`.

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
