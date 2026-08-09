-- ============================================================
-- Structured extraction targets (docs/STRUCTURED.md §2).
--
-- herald-extract reads whole-table chunks (kind='table') that hold teacher
-- salary schedules and stipend schedules and fills these normalized tables, so
-- the analytical Ask path can answer parametric questions with a QUERY instead
-- of RAG ("steepest MA+30 step 10->20 across districts").
--
-- Every row carries provenance (document_id + page) so computed answers cite
-- sources like RAG answers do, and both the normalized value and the raw label
-- are stored so normalization is auditable.
--
-- Apply:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0004_structured.sql
-- ============================================================

-- Teacher salary schedules: (school_year, lane, step) -> salary --------------
create table if not exists salary_schedule (
  id             uuid primary key default gen_random_uuid(),
  district_id    uuid not null references districts(id) on delete cascade,
  document_id    uuid not null references documents(id) on delete cascade,
  page           int,
  school_year    text not null,        -- '2024-25' (the year this grid applies)
  lane           text not null,        -- canonical, e.g. 'MA+30' (or 'other')
  lane_raw       text not null,        -- as printed
  step           int not null,         -- step number as printed
  years_service  int,                  -- mapped years, when the contract states it
  is_longevity   boolean not null default false,
  salary         numeric not null,
  notes          text,
  created_at     timestamptz not null default now(),
  unique (district_id, school_year, lane, step)
);

create index if not exists salary_schedule_lookup_idx
  on salary_schedule (district_id, lane, step);

-- Stipend schedules: coaches, co-curricular, extra-duty ----------------------
-- school_year and tier default to '' (not null) so the unique key is reliable:
-- NULLs compare distinct in Postgres, which would let re-runs duplicate rows.
create table if not exists stipend_schedule (
  id           uuid primary key default gen_random_uuid(),
  district_id  uuid not null references districts(id) on delete cascade,
  document_id  uuid not null references documents(id) on delete cascade,
  page         int,
  school_year  text not null default '',
  category     text,             -- 'athletics' | 'cocurricular' | 'extra_duty'
  position     text not null,    -- canonical, e.g. 'Head Football Coach'
  position_raw text not null,
  tier         text not null default '',   -- level/experience tier, '' if none
  amount       numeric,          -- flat dollars, or the low end of a range
  amount_high  numeric,          -- high end if a range
  amount_pct   numeric,          -- the percent, when amount_basis='percent_of_base'
  amount_basis text,             -- 'flat' | 'range' | 'percent_of_base'
  notes        text,
  created_at   timestamptz not null default now(),
  unique (district_id, school_year, position, tier)
);

create index if not exists stipend_schedule_lookup_idx
  on stipend_schedule (district_id, position);

-- Resumability: stamp a table chunk once herald-extract has processed it, so a
-- re-run skips it (whether or not it yielded structured rows) instead of paying
-- for the LLM again. A fetch/parse failure leaves it null so a later run retries.
alter table chunks add column if not exists extracted_at timestamptz;
