-- Schools corpus RPCs for the Next.js app (v1: ask + full-text search).
--
-- Mirrors herald.schools_retrieval: the ask path ranks **per district**
-- (row_number over partition by district) so one verbose district can't crowd
-- out the panel and absence is reportable; the search path is a plain global
-- FTS for the "find every mention of X" box. All the heavy lifting stays in
-- Postgres where the HNSW + GIN indexes live.
--
-- Apply against the schools database:
--   psql "$SUPABASE_DB_URL" -f web/supabase/migrations/20260724_schools_rpcs.sql

drop function if exists match_school_chunks_semantic(vector, int, text[], text, date, date);
drop function if exists match_school_chunks_fts(text, int, text[], text, date, date);
drop function if exists search_school_chunks(text, int, text[]);

-- Per-district semantic leg: top `per_district` chunks by cosine distance.
create or replace function match_school_chunks_semantic(
  query_embedding vector(1024),
  per_district int default 12,
  filter_districts text[] default null,
  filter_doc_type text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  chunk_id uuid, district text, meeting_date date, doc_type text,
  doc_title text, section_path text, heading text, content text,
  source_url text, score float
)
language sql stable security definer set statement_timeout = '60s'
as $$
  select t.id, t.slug, t.meeting_date, t.doc_type, t.title,
         t.section_path, t.heading, t.content, t.source_url,
         (1 - t.distance)::float as score
  from (
    select c.id, d.slug, c.meeting_date, c.doc_type, doc.title,
           c.section_path, c.heading, c.content, doc.source_url,
           c.embedding <=> query_embedding as distance,
           row_number() over (
             partition by c.district_id
             order by c.embedding <=> query_embedding
           ) as rn
    from chunks c
    join districts d   on d.id = c.district_id
    join documents doc on doc.id = c.document_id
    where c.status = 'active' and c.embedding is not null
      and (filter_districts is null or d.slug = any(filter_districts))
      and (filter_doc_type is null or c.doc_type = filter_doc_type)
      and (filter_date_from is null or c.meeting_date >= filter_date_from)
      and (filter_date_to is null or c.meeting_date <= filter_date_to)
  ) t
  where t.rn <= per_district
  order by t.slug, t.distance;
$$;

-- Per-district FTS leg: top `per_district` chunks by text rank.
create or replace function match_school_chunks_fts(
  query text,
  per_district int default 12,
  filter_districts text[] default null,
  filter_doc_type text default null,
  filter_date_from date default null,
  filter_date_to date default null
)
returns table (
  chunk_id uuid, district text, meeting_date date, doc_type text,
  doc_title text, section_path text, heading text, content text,
  source_url text, score float
)
language sql stable security definer set statement_timeout = '60s'
as $$
  select t.id, t.slug, t.meeting_date, t.doc_type, t.title,
         t.section_path, t.heading, t.content, t.source_url, t.rank::float
  from (
    select c.id, d.slug, c.meeting_date, c.doc_type, doc.title,
           c.section_path, c.heading, c.content, doc.source_url,
           ts_rank_cd(c.fts, q) as rank,
           row_number() over (
             partition by c.district_id
             order by ts_rank_cd(c.fts, q) desc
           ) as rn
    from chunks c
    join districts d   on d.id = c.district_id
    join documents doc on doc.id = c.document_id,
         websearch_to_tsquery('english', query) q
    where c.status = 'active' and c.fts @@ q
      and (filter_districts is null or d.slug = any(filter_districts))
      and (filter_doc_type is null or c.doc_type = filter_doc_type)
      and (filter_date_from is null or c.meeting_date >= filter_date_from)
      and (filter_date_to is null or c.meeting_date <= filter_date_to)
  ) t
  where t.rn <= per_district
  order by t.slug, t.rank desc;
$$;

-- Global full-text search for the search box ("every mention of X"),
-- ranked across the whole corpus (not per district).
create or replace function search_school_chunks(
  query text,
  match_count int default 60,
  filter_districts text[] default null
)
returns table (
  chunk_id uuid, district text, meeting_date date, doc_type text,
  doc_title text, section_path text, heading text, content text,
  source_url text, rank float
)
language sql stable security definer set statement_timeout = '30s'
as $$
  select c.id, d.slug, c.meeting_date, c.doc_type, doc.title,
         c.section_path, c.heading, c.content, doc.source_url,
         ts_rank_cd(c.fts, q)::float as rank
  from chunks c
  join districts d   on d.id = c.district_id
  join documents doc on doc.id = c.document_id,
       websearch_to_tsquery('english', query) q
  where c.status = 'active' and c.fts @@ q
    and (filter_districts is null or d.slug = any(filter_districts))
  order by rank desc
  limit match_count;
$$;
