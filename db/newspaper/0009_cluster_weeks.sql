-- Per-cluster per-week aggregates for the Cluster Dossier page.
--
-- One row per (cluster, ISO week). Populated by
-- scripts/cluster_weeks.py from status='active' AND content_type=0
-- chunks only — consistent with the quarantine everywhere else.
--
-- centroid_x / centroid_y are the mean of the week's member chunks'
-- already-stored UMAP coordinates (mean-of-projections, NOT
-- projection-of-mean): no model persistence needed, and the weekly
-- centroid sits at the visual center of the dots the user can see on
-- the explore map.
--
-- top_terms: ~5 strings from per-week c-TF-IDF computed WITHIN the
-- cluster (this week's chunks vs the cluster's other weeks), so terms
-- distinguish this week of the story from the story's other weeks.

create table if not exists cluster_weeks (
  id                uuid primary key default gen_random_uuid(),
  cluster_id        uuid not null references clusters(id) on delete cascade,
  week_start        date not null,            -- Monday of the ISO week
  chunk_count       int not null default 0,
  count_by_paper    jsonb not null default '{}',  -- {lccn: count}
  mean_ocr_quality  real,                     -- null when no scored chunks
  centroid_x        real,
  centroid_y        real,
  top_terms         jsonb not null default '[]',  -- ["term", ...]
  unique (cluster_id, week_start)
);

create index if not exists cluster_weeks_cluster_idx
  on cluster_weeks (cluster_id, week_start);
