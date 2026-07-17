-- ============================================================
-- Westchester schools corpus — initial schema
--
-- Tables: districts, documents, chunks.
-- Implements the design in docs/CHUNKING.md ("Proposed schema"):
-- district / meeting_date / doc_type are DENORMALIZED onto chunks so
-- the two hard requirements — chronological ordering and district
-- filtering — are single-column operations that compose with vector
-- search as cheap metadata filters.
--
-- chunks.embedding is vector(1024), sized for Voyage-3-family models.
-- Changing embedding models requires a migration + full re-embed;
-- never mix versions in one HNSW index.
--
-- The newspaper engine's original schema lives in db/newspaper/ for
-- reference; this chain targets a fresh database and stands alone.
--
-- Apply with:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0001_schools_init.sql
-- or, from a GitHub Actions runner without psql:
--   herald-ingest init-db
-- ============================================================

create extension if not exists vector;     -- pgvector
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- districts ----------------------------------------------------
create table if not exists districts (
  id             uuid primary key default gen_random_uuid(),
  slug           text unique not null,      -- e.g. 'port-chester-rye'
  name           text not null,
  website        text,
  boarddocs_slug text,
  created_at     timestamptz not null default now()
);

-- documents ------------------------------------------------------
-- One row per scraped artifact (PDF). Mirrors the scrape ManifestEntry
-- plus what ingest learns from the content (meeting_date, page_count).
create table if not exists documents (
  id            uuid primary key default gen_random_uuid(),
  district_id   uuid not null references districts(id) on delete cascade,
  doc_type      text not null,
  title         text not null,
  source_url    text not null,
  sha256        text not null,
  size_bytes    bigint,
  content_type  text,
  local_path    text,
  committee     text,
  meeting_id    text,
  meeting_date  date,           -- authoritative: parsed from content at ingest
  page_count    int,
  text_chars    int,
  ingest_status text not null default 'pending',
  ingest_error  text,
  fetched_at    timestamptz,
  ingested_at   timestamptz,
  created_at    timestamptz not null default now(),
  unique (district_id, sha256),
  constraint documents_doc_type_chk check (doc_type in
    ('minutes','agenda','policy','handbook','contract','budget','transcript','other')),
  constraint documents_ingest_status_chk check (ingest_status in
    ('pending','ingested','no_text','error'))
);
create index if not exists documents_district_date_idx
  on documents (district_id, meeting_date);
create index if not exists documents_type_idx on documents (doc_type);

-- chunks ---------------------------------------------------------
-- The retrieval unit. section_path is the document's own outline address
-- ("P13.D" = item D of part 13), section_type the human topic label.
create table if not exists chunks (
  id            uuid primary key default gen_random_uuid(),
  document_id   uuid not null references documents(id) on delete cascade,
  chunk_index   int  not null,   -- order within the document
  section_path  text not null,
  section_type  text,
  heading       text,
  content       text not null,
  embedding     vector(1024),
  fts           tsvector generated always as (to_tsvector('english', content)) stored,
  -- denormalized from the document (see header comment)
  district_id   uuid not null references districts(id) on delete cascade,
  meeting_date  date,
  doc_type      text,
  -- quality gate (populated by a later scoring pass; ingest writes 'active')
  status        text not null default 'active',
  quality_score real,
  created_at    timestamptz not null default now(),
  unique (document_id, chunk_index),
  constraint chunks_status_chk check (status in ('active','quarantined'))
);
create index if not exists chunks_hnsw_idx on chunks
  using hnsw (embedding vector_cosine_ops)
  where status = 'active';
create index if not exists chunks_fts_idx on chunks
  using gin (fts)
  where status = 'active';
create index if not exists chunks_district_date_idx
  on chunks (district_id, meeting_date);
create index if not exists chunks_type_idx on chunks (doc_type);
