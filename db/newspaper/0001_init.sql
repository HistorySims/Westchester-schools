-- ============================================================
-- FORK TODO (herald): schema: papers/issues/pages -> districts/documents; drop LoC-specific columns
-- Inherited from Herald; rewrite for this corpus before use.
-- See docs/ENGINE_EXTRACTION.md.
-- ============================================================
-- ============================================================
-- Herald — initial schema (Phase 1)
--
-- Tables: papers, users, issues, pages, chunks, credit_ledger, reocr_jobs
-- See docs/PLAN.md §5 for rationale and index notes.
--
-- NOTE: chunks.embedding is vector(1024), sized for Voyage-3-family
-- embedding models. Changing embedding models (e.g. dimensionality or
-- vendor) requires a migration and a full re-embed of the corpus —
-- never mix versions in one HNSW index. See docs/PLAN.md §7.
--
-- Apply with:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0001_init.sql
-- ============================================================

-- Extensions ---------------------------------------------------
create extension if not exists vector;     -- pgvector
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- Shared trigger function for auto-maintained updated_at columns
create or replace function set_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- papers -------------------------------------------------------
create table if not exists papers (
  id          uuid primary key default gen_random_uuid(),
  lccn        text unique not null,
  title       text not null,
  place       text,
  start_year  int,
  end_year    int,
  created_at  timestamptz not null default now()
);

-- users (Phase 3-active; schema lives in Phase 1) --------------
create table if not exists users (
  id                uuid primary key default gen_random_uuid(),
  email             text unique,
  display_name      text,
  credits_remaining int  not null default 0,
  created_at        timestamptz not null default now()
);

-- issues -------------------------------------------------------
create table if not exists issues (
  id          uuid primary key default gen_random_uuid(),
  paper_id    uuid not null references papers(id) on delete cascade,
  date_issued date not null,
  edition     int  not null default 1,
  loc_url     text not null,
  created_at  timestamptz not null default now(),
  unique (paper_id, date_issued, edition)
);
create index if not exists issues_date_idx       on issues (date_issued);
create index if not exists issues_paper_date_idx on issues (paper_id, date_issued);

-- pages --------------------------------------------------------
create table if not exists pages (
  id              uuid primary key default gen_random_uuid(),
  issue_id        uuid not null references issues(id) on delete cascade,
  sequence        int  not null,
  image_url       text not null,
  jp2_url         text,
  pdf_url         text,
  ocr_text        text,
  ocr_version     int  not null default 1,
  ocr_source      text not null default 'loc',
  cleaned_at      timestamptz,
  cleaned_by_user uuid references users(id),
  reocr_status    text not null default 'original',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (issue_id, sequence),
  constraint pages_reocr_status_chk
    check (reocr_status in ('original','pending','cleaned','failed')),
  constraint pages_ocr_source_chk
    check (ocr_source in ('loc','claude-vision','gpt-vision','other'))
);
create index if not exists pages_reocr_idx on pages (reocr_status)
  where reocr_status <> 'original';

-- Auto-maintain updated_at on pages
drop trigger if exists pages_set_updated_at on pages;
create trigger pages_set_updated_at
  before update on pages
  for each row execute function set_updated_at();

-- chunks -------------------------------------------------------
create table if not exists chunks (
  id           uuid primary key default gen_random_uuid(),
  page_id      uuid not null references pages(id) on delete cascade,
  ocr_version  int  not null,
  chunk_index  int  not null,
  content      text not null,
  word_start   int  not null,
  word_end     int  not null,
  embedding    vector(1024),
  fts          tsvector generated always as (to_tsvector('english', content)) stored,
  is_current   boolean not null default true,
  created_at   timestamptz not null default now(),
  unique (page_id, ocr_version, chunk_index)
);
create index if not exists chunks_hnsw_idx on chunks
  using hnsw (embedding vector_cosine_ops)
  where is_current = true;
create index if not exists chunks_fts_idx on chunks
  using gin (fts)
  where is_current = true;
-- No separate chunks_page_idx: the unique constraint on
-- (page_id, ocr_version, chunk_index) already provides a btree
-- whose leading column is page_id. Drop it explicitly in case an
-- earlier version of this migration created one.
drop index if exists chunks_page_idx;

-- credit_ledger (append-only) ----------------------------------
create table if not exists credit_ledger (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references users(id) on delete cascade,
  delta      int  not null,
  reason     text not null,
  page_id    uuid references pages(id),
  created_at timestamptz not null default now(),
  constraint credit_ledger_reason_chk
    check (reason in ('signup_bonus','purchase','reocr_page','refund','admin_adjust'))
);
create index if not exists credit_ledger_user_idx
  on credit_ledger (user_id, created_at desc);

-- reocr_jobs ---------------------------------------------------
create table if not exists reocr_jobs (
  id               uuid primary key default gen_random_uuid(),
  page_id          uuid not null references pages(id) on delete cascade,
  requested_by     uuid not null references users(id),
  status           text not null default 'queued',
  model            text,
  prev_ocr_version int  not null,
  new_ocr_version  int,
  error            text,
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz not null default now(),
  constraint reocr_jobs_status_chk
    check (status in ('queued','running','succeeded','failed'))
);
-- at most one in-flight cleanup per (page, version)
create unique index if not exists reocr_jobs_inflight_idx
  on reocr_jobs (page_id, prev_ocr_version)
  where status in ('queued','running');
