-- Storage for topic-map snapshots the web /explore page renders.
--
-- `herald-cluster run --publish` inserts one row per run (the full per-chunk
-- export as jsonb); the web app reads the most recent. Append-only so history
-- is kept and a bad run can be rolled back by deleting its row.
--
-- Apply against the schools database (once):
--   psql "$SUPABASE_DB_URL" -f web/supabase/migrations/20260725_cluster_maps.sql

create table if not exists cluster_maps (
  id           uuid primary key default gen_random_uuid(),
  created_at   timestamptz not null default now(),
  generated_at text,
  n_points     int,
  n_clusters   int,
  data         jsonb not null
);

create index if not exists cluster_maps_created_idx on cluster_maps (created_at desc);
