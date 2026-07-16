-- Chunk quality + quarantine columns.
--
-- Adds a unified quarantine layer to chunks. Retrieval (semantic +
-- FTS RPCs) is gated on status='active'. The backfill script
-- scripts/score_chunk_quality.py populates status, quality_score,
-- and quality_subscores from the heuristic in src/herald/classify.py.
--
-- New chunks created by ingest or by Phase-3 re-OCR default to
-- 'active' and get scored on the next run of the workflow.

alter table chunks
  add column if not exists status text not null default 'active',
  add column if not exists quality_score real,
  add column if not exists quality_subscores jsonb,
  add column if not exists quarantined_at timestamptz,
  add column if not exists quarantine_reason text;

alter table chunks
  drop constraint if exists chunks_status_chk;
alter table chunks
  add constraint chunks_status_chk
  check (status in ('active', 'quarantined'));

-- Partial index so the active pipeline can fast-path WHERE status='active'.
create index if not exists chunks_active_idx
  on chunks (status)
  where status = 'active';

-- Update retrieval RPCs to filter by chunks.status = 'active'.
-- Drop-and-recreate keeps the function signature compatible with the
-- web client (web/src/lib/retrieval.ts).

drop function if exists match_chunks_semantic(vector, int, text, date, date);
drop function if exists match_chunks_fts(text, int, text, date, date);

create or replace function match_chunks_semantic(
  query_embedding vector(1024),
  match_count int default 20,
  filter_paper_lccn text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  chunk_id uuid,
  content text,
  page_id uuid,
  paper_lccn text,
  paper_title text,
  date_issued date,
  edition int,
  page_sequence int,
  image_url text,
  resource_url text,
  similarity float
)
language plpgsql stable
security definer
set statement_timeout = '60s'
as $$
begin
  return query
  select
    chunks.id as chunk_id,
    chunks.content,
    pages.id as page_id,
    papers.lccn as paper_lccn,
    papers.title as paper_title,
    issues.date_issued,
    issues.edition,
    pages.sequence as page_sequence,
    pages.image_url,
    coalesce(pages.iiif_info_url, '') as resource_url,
    (1 - (chunks.embedding <=> query_embedding))::float as similarity
  from chunks
  join pages on pages.id = chunks.page_id
  join issues on issues.id = pages.issue_id
  join papers on papers.id = issues.paper_id
  where chunks.is_current = true
    and chunks.status = 'active'
    and (filter_paper_lccn is null or papers.lccn = filter_paper_lccn)
    and (filter_date_from is null or issues.date_issued >= filter_date_from)
    and (filter_date_to is null or issues.date_issued <= filter_date_to)
  order by chunks.embedding <=> query_embedding
  limit match_count;
end;
$$;

create or replace function match_chunks_fts(
  query text,
  match_count int default 20,
  filter_paper_lccn text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  chunk_id uuid,
  content text,
  page_id uuid,
  paper_lccn text,
  paper_title text,
  date_issued date,
  edition int,
  page_sequence int,
  image_url text,
  resource_url text,
  rank float
)
language plpgsql stable
security definer
set statement_timeout = '60s'
as $$
begin
  return query
  select
    chunks.id as chunk_id,
    chunks.content,
    pages.id as page_id,
    papers.lccn as paper_lccn,
    papers.title as paper_title,
    issues.date_issued,
    issues.edition,
    pages.sequence as page_sequence,
    pages.image_url,
    coalesce(pages.iiif_info_url, '') as resource_url,
    ts_rank_cd(chunks.fts, websearch_to_tsquery('english', query))::float as rank
  from chunks
  join pages on pages.id = chunks.page_id
  join issues on issues.id = pages.issue_id
  join papers on papers.id = issues.paper_id
  where chunks.is_current = true
    and chunks.status = 'active'
    and chunks.fts @@ websearch_to_tsquery('english', query)
    and (filter_paper_lccn is null or papers.lccn = filter_paper_lccn)
    and (filter_date_from is null or issues.date_issued >= filter_date_from)
    and (filter_date_to is null or issues.date_issued <= filter_date_to)
  order by rank desc
  limit match_count;
end;
$$;
