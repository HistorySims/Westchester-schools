# Herald — Hierarchical Clustering & Explore Visualization

## Context

The Herald corpus has ~130k-180k text chunks with 1024-dim Voyage embeddings from two New York newspapers (June-September 1845). The existing search interface lets users ask questions and get synthesized answers. But there's no way to see the *shape* of the corpus — which stories got the most coverage, how topics cluster, what's noise (ads, legal notices, garbled OCR) vs. signal.

This plan adds: (1) hierarchical clustering with population-weighted centroids so large topics dominate, (2) temporal awareness so clusters know their date range, (3) an interactive `/explore` page with a WebGL scatter plot of every chunk in the corpus, and (4) automatic filtering of ads/legal/bad-OCR content.

---

## Architecture Overview

Three subsystems:

```
Python CLI batch job          Supabase (new tables)         Next.js /explore page
─────────────────────         ──────────────────────        ─────────────────────
herald cluster                cluster_runs                  deck.gl scatter plot
  ├─ load embeddings          clusters (per tier)           ├─ binary point data (~3MB)
  ├─ HDBSCAN (tier 0)        chunk_projections             ├─ tier selector
  ├─ agglomerative merge      active_cluster_run            ├─ content type filter
  │   (tiers 1-3)                                           ├─ time slider
  ├─ UMAP → 2D coords                                      ├─ click → chunk detail
  ├─ content classification                                 └─ link to source page
  └─ write to DB
```

---

## 1. Clustering Algorithm

**HDBSCAN** on the full 1024-dim embeddings for natural base clusters (tier 0), then **scipy agglomerative merging** with size-weighted centroids for tiers 1-3.

Why HDBSCAN: finds natural density-based clusters of varying sizes without a predetermined count. Perfect for messy OCR corpus where cluster sizes should be uneven. Why not cluster on reduced dims: HDBSCAN handles 1024 dims fine for 180k points, and reducing first would couple clustering topology with visualization layout.

**4 tiers:**
| Tier | Target count | Purpose |
|------|-------------|---------|
| 0 (leaf) | ~500-2000 | Natural HDBSCAN clusters (min_cluster_size=25) |
| 1 (medium) | ~80-150 | Topic groups |
| 2 (large) | ~15-25 | Major themes |
| 3 (macro) | ~3-7 | Broadest categories |

**Weighted centroids:** When merging cluster A (1000 chunks) with cluster B (10 chunks), the merged centroid = (1000×cA + 10×cB) / 1010. This is the NYC/Syracuse/Buffalo effect — big topics pull the center toward them.

**HDBSCAN noise handling:** Noise points (label -1, typically 10-30%) get assigned to the nearest non-noise cluster by cosine distance to centroids. Every chunk gets a cluster.

**Hierarchy construction:** Compute scipy linkage matrix once on the tier-0 centroid vectors (~500-2000 points, fast). Cut at 3 heights to produce tiers 1-3. For each higher-tier cluster, compute weighted centroid from member tier-0 centroids × their sizes.

---

## 2. UMAP Projection

Single UMAP run: 1024 dims → 2D for visualization.

```python
umap.UMAP(n_components=2, n_neighbors=15, min_dist=0.1, 
          metric='cosine', random_state=42, low_memory=True)
```

~10-20 minutes on a modern laptop for 180k points. Coordinates normalized to [0, 1]. Stored per-chunk in the database.

---

## 3. Content Classification (Heuristics)

Classify each chunk as: `content` (0), `ad` (1), `legal` (2), `bad_ocr` (3).

**bad_ocr:** Non-alphabetic ratio > 40%, or average word length < 2 or > 15, or dictionary-word ratio < 30% (checked against a bundled ~20k common English word set including 1840s terms).

**ad:** Short text (< 80 words) with price patterns (`$`, `cents`, `per annum`, `dollars`), or contains advertising keywords (`FOR SALE`, `WANTED`, `TO LET`, `AUCTION`, `REWARD`).

**legal:** Contains boilerplate (`NOTICE IS HEREBY GIVEN`, `IN PURSUANCE OF`, `BY ORDER OF`, `SUPREME COURT`, `MORTGAGE`, `foreclosure`).

**content:** Everything else (default).

No LLM calls — pure heuristics, runs in seconds.

---

## 4. Database Schema

New migration: `db/migrations/0002_clustering.sql`

```sql
-- Clustering run metadata (supports re-running as corpus grows)
create table cluster_runs (
  id          uuid primary key default gen_random_uuid(),
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  chunk_count int not null,
  params      jsonb not null default '{}',
  status      text not null default 'running',
  constraint cluster_runs_status_chk
    check (status in ('running','completed','failed'))
);

-- Cluster definitions at each tier
create table clusters (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references cluster_runs(id) on delete cascade,
  tier         smallint not null,    -- 0=leaf, 1=medium, 2=large, 3=macro
  label        int not null,
  size         int not null,
  centroid     vector(1024),
  date_min     date,
  date_max     date,
  parent_id    uuid references clusters(id),
  unique (run_id, tier, label)
);

-- Per-chunk: UMAP coords + cluster labels + content type
create table chunk_projections (
  chunk_id      uuid not null references chunks(id) on delete cascade,
  run_id        uuid not null references cluster_runs(id) on delete cascade,
  x             real not null,
  y             real not null,
  cluster_t0    int not null,
  cluster_t1    int not null,
  cluster_t2    int not null,
  cluster_t3    int not null,
  content_type  smallint not null default 0,
  primary key (chunk_id, run_id)
);

-- Which run the web UI reads from (singleton)
create table active_cluster_run (
  singleton    boolean primary key default true,
  run_id       uuid not null references cluster_runs(id),
  activated_at timestamptz not null default now(),
  constraint active_cluster_run_singleton_chk check (singleton = true)
);
```

---

## 5. Python Batch Pipeline

**New files:**
- `src/herald/cluster.py` — main pipeline
- `src/herald/classify.py` — content classification heuristics

**Modified:** `src/herald/cli.py` (add `herald cluster` command)

**New dependencies** in `pyproject.toml`:
```
hdbscan>=0.8.33
umap-learn>=0.5
```

**Pipeline stages:**

1. **Load** — Read all chunk embeddings + dates via server-side cursor (10k batch)
2. **Classify** — Apply heuristics to each chunk's content text
3. **HDBSCAN** — Cluster on 1024-dim embeddings, assign noise to nearest cluster
4. **Agglomerative merge** — Build tiers 1-3 from tier-0 centroids using scipy linkage
5. **UMAP** — Project 1024-dim → 2D coordinates
6. **Write** — Insert cluster_runs, clusters, chunk_projections; activate the new run

Memory: 180k × 1024 float32 ≈ 700MB. Fits in 8GB RAM. Free embedding matrix after UMAP.

CLI command:
```
herald cluster [--min-cluster-size 25] [--umap-neighbors 15] [--umap-min-dist 0.1]
```

---

## 6. API Endpoints

**Binary point data:** `GET /api/explore/points`
- Returns packed ArrayBuffer: uint32 count + N × 17 bytes per point
- Per point: float32 x, float32 y, uint16 cluster_t0-t3, uint8 content_type
- ~3MB for 180k points. Cache-Control: public, max-age=3600.

**Cluster metadata:** `GET /api/explore/clusters?tier=<0-3>`
- JSON array of clusters at the specified tier (label, size, date_min, date_max, parent_label)

**Date data (lazy):** `GET /api/explore/dates`
- Binary uint16 date offsets (days since corpus min date), same order as /points
- ~360KB, only fetched when time filter is activated

**Chunk detail (on click):** `GET /api/explore/chunk/[id]`
- JSON: chunk_id, content snippet, paper_title, date_issued, page_sequence, image_url, cluster_labels

**Chunk ID mapping (lazy):** `GET /api/explore/chunk-ids`
- Binary array of 16-byte UUIDs, same order as /points
- Fetched after initial render, used for click-to-navigate

---

## 7. Frontend: `/explore` Page

**New dependency:** `deck.gl` v9 (WebGL scatter plot, handles 500k+ points, built-in touch zoom/pan for mobile)

**New files:**
```
web/src/app/explore/page.tsx           — page shell
web/src/components/ExploreMap.tsx       — deck.gl ScatterplotLayer
web/src/components/ExploreSidebar.tsx   — controls panel
web/src/components/TierSelector.tsx     — tier level radio buttons
web/src/components/ContentFilter.tsx    — content type toggle checkboxes
web/src/components/TimeFilter.tsx       — date range slider
web/src/components/ChunkDetail.tsx      — clicked-point detail card
web/src/lib/explore-data.ts            — binary fetch + parse
```

**Key design decisions:**
- deck.gl's `DataFilterExtension` does GPU-side filtering — toggling content types or date ranges costs zero CPU
- Color by cluster label at selected tier, using a categorical palette
- Mobile: controls below the map (flex-col), detail card as bottom sheet
- Click a dot → fetch chunk metadata → show detail card with "View page" link
- Navigation link from main page header to `/explore`

---

## 8. Implementation Phases

### Phase A: Database + Python batch pipeline
1. Add `hdbscan`, `umap-learn` to `pyproject.toml`
2. Create `db/migrations/0002_clustering.sql`
3. Create `src/herald/classify.py` (content heuristics)
4. Create `src/herald/cluster.py` (load → cluster → UMAP → classify → write)
5. Add `herald cluster` command to `src/herald/cli.py`
6. Run on real data against Supabase

### Phase B: API endpoints
7. Create `web/src/app/api/explore/points/route.ts` (binary)
8. Create `web/src/app/api/explore/clusters/route.ts` (JSON)
9. Create `web/src/app/api/explore/dates/route.ts` (binary)
10. Create `web/src/app/api/explore/chunk/[id]/route.ts` (JSON)
11. Create `web/src/lib/explore-data.ts` (binary parsing)

### Phase C: Frontend visualization
12. Install deck.gl
13. Build ExploreMap, ExploreSidebar, TierSelector, ContentFilter, TimeFilter, ChunkDetail
14. Wire up the `/explore` page
15. Add nav link from main page
16. Mobile testing

---

## 9. Verification

1. `herald cluster` completes end-to-end: cluster_runs, clusters, chunk_projections all populated
2. Tier 0 has 500-2000 clusters, tier 3 has 3-7 clusters
3. Weighted centroids verified: large clusters pull merged centroids toward them
4. `/explore` loads scatter plot with ~130k-180k colored dots in < 3 seconds
5. Tier selector changes cluster coloring
6. Content filter toggles hide/show ads, legal, bad OCR
7. Time slider filters by date range
8. Clicking a dot shows chunk detail with correct metadata
9. "View page" link navigates to the source
10. Works on mobile Safari (touch zoom/pan, responsive layout)
