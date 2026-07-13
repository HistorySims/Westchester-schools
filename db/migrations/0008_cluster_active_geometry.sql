-- Recomputed-from-active geometry for each cluster.
--
-- The original cluster_run computed centroids, date ranges, and sizes
-- before the OCR-quarantine pipeline existed. Those numbers still
-- reflect quarantined garbage. We rerun centroid + drift + burstiness
-- math over status='active' AND content_type=0 chunks only and store
-- the results in these columns. The brief/explore/labeling code
-- prefers active_centroid + active_size when present.
--
-- active_centroid is nullable so the recompute can fail/be partial
-- without corrupting the original centroid column. Brief.matcher falls
-- back to centroid when active_centroid is null. After the recompute
-- runs, active_size > 0 is the eligibility filter.

alter table clusters
  add column if not exists active_size      int,
  add column if not exists active_centroid  vector(1024),
  add column if not exists burstiness       real,
  add column if not exists active_date_min  date,
  add column if not exists active_date_max  date;
