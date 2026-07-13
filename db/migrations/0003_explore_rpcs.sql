-- ============================================================
-- FORK TODO (schoolsengine): RPCs reference the schema — update table/column names to match 0001
-- Inherited from Herald; rewrite for this corpus before use.
-- See docs/ENGINE_EXTRACTION.md.
-- ============================================================
-- RPC functions for the /explore page.
-- These do the joins in Postgres so the API can fetch everything in one call.

create or replace function get_explore_dates(active_run uuid)
returns table (chunk_id uuid, date_issued date)
language sql
stable
security definer
set statement_timeout = '60s'
as $$
  select cp.chunk_id, i.date_issued
  from chunk_projections cp
  join chunks c on c.id = cp.chunk_id
  join pages  p on p.id = c.page_id
  join issues i on i.id = p.issue_id
  where cp.run_id = active_run
  order by cp.chunk_id
$$;

grant execute on function get_explore_dates(uuid) to anon, authenticated, service_role;
