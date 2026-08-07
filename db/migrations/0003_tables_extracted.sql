-- ============================================================
-- Resumability marker for the source_url table backfill (herald-ingest
-- tables-db).
--
-- The backfill re-fetches every ingested document's PDF and extracts its
-- tables. It is large and slow (thousands of docs, some multi-minute budget
-- books), so it must survive timeouts and re-runs without (a) re-fetching
-- documents it already processed or (b) re-inserting duplicate table chunks.
--
-- A NULL/empty table-chunk count can't distinguish "not processed yet" from
-- "processed, no tables found" — most documents (minutes, policies) have no
-- tables. So we stamp each document once its tables have been extracted,
-- whatever the count. The backfill's candidate query is `tables_extracted_at
-- is null`; a fetch failure (e.g. a 403) leaves it null so a later run retries.
--
-- Apply:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0003_tables_extracted.sql
-- ============================================================

alter table documents add column if not exists tables_extracted_at timestamptz;

-- Documents that already carry table chunks (from the earlier manifest-based
-- backfill) are done — stamp them so tables-db skips them instead of
-- re-fetching and appending duplicate table chunks.
update documents d set tables_extracted_at = now()
where tables_extracted_at is null
  and exists (select 1 from chunks c where c.document_id = d.id and c.kind = 'table');
