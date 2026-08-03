-- ============================================================
-- Table-aware chunking: mark whether a chunk is prose or a whole table.
--
-- Tables (salary schedules, budgets, stipend appendices) are kept as single
-- chunks (kind='table') instead of being fragmented, so retrieval finds the
-- whole grid and the structured-extraction pass (docs/STRUCTURED.md) gets a
-- clean, header-bearing input. Prose is unchanged (kind='prose', the default).
--
-- Apply:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0002_chunk_kind.sql
-- ============================================================

alter table chunks add column if not exists kind text not null default 'prose';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'chunks_kind_chk'
  ) then
    alter table chunks add constraint chunks_kind_chk check (kind in ('prose', 'table'));
  end if;
end $$;

create index if not exists chunks_kind_idx on chunks (kind) where kind = 'table';
