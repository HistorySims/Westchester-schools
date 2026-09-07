-- ============================================================
-- chunks.embedding: vector(1024) -> halfvec(1024)
--
-- The corpus reached 791 MB against a 500 MB free tier, and 366 MB of that
-- was one index: chunks_hnsw_idx. HNSW is not a pointer structure — it stores
-- a full copy of every vector inside the index, because the graph walk
-- computes distances as it goes and cannot afford a heap lookup per hop. At
-- vector(1024) that is 4,100 bytes per row in the index and another 4,100 in
-- the table's TOAST.
--
-- halfvec(1024) stores fp16 instead of fp32: 2,050 bytes. Voyage's vectors
-- are normalized and fp16 has ample precision for cosine distance at this
-- dimension, so the recall cost is expected to be well under a point. That is
-- an estimate — re-run the eval set after this lands to confirm it.
--
-- Requires pgvector >= 0.7.0 for the halfvec type. Supabase is on 0.8.2.
--
-- The full plan this belongs to, including what happens to the index, is in
-- docs/DISK_SPACE.md.
--
-- Apply:
--   herald-migrate apply          (Actions -> migrate -> Run workflow)
-- ============================================================

-- This rewrites every row in chunks plus ~195 MB of TOAST, which takes
-- minutes. Pasting it into the Supabase SQL editor fails with "Load failed
-- (api.supabase.com)" — the dashboard's HTTP layer times out long before the
-- statement finishes, leaving no way to tell whether it rolled back or is
-- still holding an ACCESS EXCLUSIVE lock. Running it from a runner over a
-- direct connection avoids that entirely; clearing the timeout covers the
-- case where the role carries a non-zero default.
set statement_timeout = 0;

-- chunks_hnsw_idx is declared `using hnsw (embedding vector_cosine_ops)`, and
-- vector_cosine_ops does not apply to halfvec — the type change cannot
-- proceed while it exists. Dropping it loses nothing: the index is entirely
-- derived from the column, no embeddings are deleted and no Voyage calls are
-- re-spent.
--
-- It is deliberately NOT recreated here. At ~138 MB on halfvec it would put
-- the database back near the tier limit, and brute-force cosine over this
-- corpus may well be fast enough to skip it. That decision is a measurement,
-- not a guess, so it is made after the import rather than assumed here.
drop index if exists chunks_hnsw_idx;

-- Guarded so re-running is a no-op rather than an error. 0005 taught this the
-- hard way: it had been applied by hand before schema_migrations existed, so
-- the runner had no record of it and re-raised DuplicateTable.
do $$
begin
  if exists (
    select 1
    from pg_attribute a
    join pg_type t on t.oid = a.atttypid
    where a.attrelid = 'chunks'::regclass
      and a.attname  = 'embedding'
      and t.typname  = 'vector'
  ) then
    alter table chunks
      alter column embedding type halfvec(1024)
      using embedding::halfvec(1024);
  end if;
end $$;
