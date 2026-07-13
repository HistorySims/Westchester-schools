-- Semantic drift metrics per cluster, computed by
-- scripts/cluster_drift.py and surfaced in diagnostic reports.
--
-- drift_cumulative: sum of cosine distances between consecutive
--   weekly centroids. High = vocabulary moves week to week.
-- drift_net: cosine distance from first-week centroid to last-week
--   centroid. High = net displacement (story drifted in one direction
--   rather than oscillating).
-- drift_weeks: number of weeks with at least one member chunk.

alter table clusters
  add column if not exists drift_cumulative real,
  add column if not exists drift_net        real,
  add column if not exists drift_weeks      int;
