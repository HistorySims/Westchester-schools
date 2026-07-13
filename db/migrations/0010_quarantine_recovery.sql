-- Quarantine Recovery System — Phase A storage.
--
-- Targeting, not reading. Each quarantined chunk gets per-signal
-- component scores stored individually so question-scoped scoring
-- can reweight at query time. All weights and thresholds live in
-- Python constants (scripts/recovery_score.py).

create extension if not exists pg_trgm;

-- A1 — Entity gazetteer learned from status='active' chunks.
-- Recurring capitalized tokens (≥3 distinct chunks) plus multi-word
-- capitalized spans. No NER model.
create table if not exists entity_gazetteer (
  id            uuid primary key default gen_random_uuid(),
  surface       text not null unique,
  freq          int  not null,                -- distinct chunks containing it
  cluster_t0    int[] not null default '{}',  -- t0 labels it appears in
  is_multiword  boolean not null default false,
  created_at    timestamptz not null default now()
);
create index if not exists entity_gazetteer_surface_trgm_idx
  on entity_gazetteer using gin (surface gin_trgm_ops);

-- A2 — Per-chunk surviving legible fragments from quarantined text.
-- kind: 'dict' (in 1840s wordlist) or 'capital' (capitalized ≥4 chars).
create table if not exists quarantine_fragments (
  id         uuid primary key default gen_random_uuid(),
  chunk_id   uuid not null references chunks(id) on delete cascade,
  fragment   text not null,
  kind       text not null check (kind in ('dict','capital')),
  position   int  not null,                   -- token index within chunk
  unique (chunk_id, fragment, position)
);
create index if not exists quarantine_fragments_chunk_idx
  on quarantine_fragments (chunk_id);
create index if not exists quarantine_fragments_trgm_idx
  on quarantine_fragments using gin (fragment gin_trgm_ops);

-- A3 — Materialized fuzzy matches between fragments and gazetteer
-- entries. via_variant: 'direct' or the damage-substitution that
-- bridged the match ('long_s_f', 'h_b', 'rn_m', …) so we can audit
-- where signal comes from.
create table if not exists quarantine_entity_matches (
  chunk_id        uuid not null references chunks(id) on delete cascade,
  entity_surface  text not null,
  fragment        text not null,
  similarity      real not null,
  via_variant     text not null default 'direct',
  primary key (chunk_id, entity_surface, fragment)
);
create index if not exists quarantine_entity_matches_chunk_idx
  on quarantine_entity_matches (chunk_id);
create index if not exists quarantine_entity_matches_entity_idx
  on quarantine_entity_matches (entity_surface);

-- A4 — Layout grid: per (paper, page_sequence, position_bucket) the
-- distribution of t0 cluster assignments and content types over
-- status='active' chunks. A slot is "regular" when its top_label_share
-- ≥ GRID_REGULAR_THRESHOLD (Python constant; default 0.60).
create table if not exists layout_slots (
  paper_id          uuid not null references papers(id) on delete cascade,
  page_sequence     int  not null,
  position_bucket   int  not null check (position_bucket between 0 and 4),
  top_label         int,                      -- top cluster_t0 label
  top_label_share   real,
  top_content_type  int,
  top_content_share real,
  sample_size       int  not null,
  primary key (paper_id, page_sequence, position_bucket)
);

-- A7 + sidecar for everything per-chunk. One row per quarantined chunk
-- with per-signal components AND the composite, so question-scoped
-- scoring at /api/brief time can reweight without re-running the batch.
create table if not exists chunk_recovery (
  chunk_id                  uuid primary key references chunks(id) on delete cascade,
  -- A3 entity
  entity_match_score        real not null default 0,
  best_entity               text,
  best_entity_similarity    real,
  best_entity_fragment      text,
  -- A4 grid
  grid_section_guess        int,             -- imputed cluster_t0 label
  grid_confidence           real not null default 0,
  grid_violation            boolean not null default false,
  -- A5 footprint / gap
  footprint_score           real not null default 0,
  footprint_cluster_label   int,
  gap_candidate_cluster_id  uuid references clusters(id),
  -- A6 quality-weighted proximity
  nearest_cluster_label     int,
  nearest_distance          real,
  weighted_proximity        real not null default 0,
  -- A7 composite
  relevance_prior           real not null default 0,
  recoverability            real not null default 0,
  gap_bonus                 real not null default 1.0,
  recovery_value            real not null default 0,
  computed_at               timestamptz not null default now()
);
create index if not exists chunk_recovery_value_idx
  on chunk_recovery (recovery_value desc);
create index if not exists chunk_recovery_gap_idx
  on chunk_recovery (gap_candidate_cluster_id)
  where gap_candidate_cluster_id is not null;
