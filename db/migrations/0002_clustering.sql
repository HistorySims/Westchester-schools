-- Hierarchical clustering + UMAP projection tables.
-- Supports re-running as the corpus grows; the web UI reads from
-- the active_cluster_run singleton pointer.

create table if not exists cluster_runs (
  id          uuid primary key default gen_random_uuid(),
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  chunk_count int not null,
  params      jsonb not null default '{}',
  status      text not null default 'running',
  constraint cluster_runs_status_chk
    check (status in ('running', 'completed', 'failed'))
);

create table if not exists clusters (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references cluster_runs(id) on delete cascade,
  tier         smallint not null,
  label        int not null,
  size         int not null,
  centroid     vector(1024),
  date_min     date,
  date_max     date,
  parent_id    uuid references clusters(id),
  unique (run_id, tier, label)
);
create index if not exists clusters_run_tier_idx on clusters (run_id, tier);

create table if not exists chunk_projections (
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
create index if not exists chunk_proj_run_idx on chunk_projections (run_id);

create table if not exists active_cluster_run (
  singleton    boolean primary key default true,
  run_id       uuid not null references cluster_runs(id),
  activated_at timestamptz not null default now(),
  constraint active_cluster_run_singleton_chk check (singleton = true)
);
