# Reclaiming database space

Working plan, opened 2026-09-07. Delete this file once the corpus is back
under the tier limit and the halfvec migration has landed.

## Situation

```
total    791 MB
chunks   47,510
```

Supabase's free tier is 500 MB. Going over is survivable for a week or so —
it warns, then pauses the project — so this is urgent but not an emergency.

The 278-agenda snapshot in `data/snapshots/boarddocs-agendas.jsonl.gz` is
committed and ready to import, but it would add roughly 127 MB at the current
per-chunk cost. **Do not run `agendas` with `dry_run: false` until this plan
is done.** A `dry_run: true` pass is safe and useful: it reports the real
chunk count for those documents instead of the projection below.

## Where the space is

Measured, `pg_stat_user_indexes` on `chunks`:

| index | size |
|---|---:|
| `chunks_hnsw_idx` | **366 MB** |
| `chunks_fts_idx` | 27 MB |
| `chunks_document_id_chunk_index_key` | 2,712 kB |
| `chunks_pkey` | 2,272 kB |
| `chunks_district_date_idx` | 568 kB |
| `chunks_type_idx` | 480 kB |
| `chunks_kind_idx` | 120 kB |

One index is 46% of the database. Everything else in that table adds to 33 MB.

### Why the HNSW index is so large

Most Postgres indexes are pointers into the heap. HNSW is not: it stores a
full copy of every vector *inside the index*, because the graph walk computes
distances as it goes and cannot afford a heap lookup at each hop. At
`vector(1024)` that is 4,100 bytes per row before a single neighbor link.

47,510 × 4,100 B ≈ 195 MB floor. Freshly built, with link lists and page
overhead, expect 215–240 MB.

It is at 366 MB. The gap — call it 130–150 MB — is bloat. pgvector's HNSW
cannot reclaim space from deleted or updated elements; `vacuum` will not
return it. This table has been churned hard (reclassify moves, re-ingests,
the orphan cleanup), and every one of those left dead graph elements behind.

## Plan

Order matters: each step makes the next one affordable.

### 1. Drop the HNSW index

```sql
drop index chunks_hnsw_idx;
```

791 MB → ~425 MB immediately. Under the limit, with headroom.

Prefer this over `reindex`, which builds the replacement *before* dropping the
original — peak usage over 1 GB, exactly the wrong direction on a project
that is already over.

Nothing is lost. The index is entirely derived from `chunks.embedding`, which
is untouched. No embeddings are deleted and no Voyage calls are re-spent.

While it is gone:

- Semantic search still returns correct results, by sequential scan over all
  47,510 vectors. Seconds per query rather than milliseconds.
- Keyword search is unaffected — `chunks_fts_idx` is separate and stays.
- Ingest is unaffected, and slightly faster with no index to maintain.
- Clustering and trajectory analysis are unaffected. `cluster.py` reads the
  embedding column directly and scans everything by design; it never used
  this index.

To restore it exactly as it was, if this plan is abandoned:

```sql
create index chunks_hnsw_idx on chunks
  using hnsw (embedding vector_cosine_ops)
  where status = 'active';
```

### 2. Convert the embedding column to halfvec

`halfvec(1024)` stores fp16 instead of fp32: 2,050 bytes per vector instead
of 4,100. Both the column and any future index halve.

Confirmed available — Supabase is on pgvector **0.8.2**, and halfvec landed in
0.7.0.

Shipped as `db/migrations/0007_embedding_halfvec.sql`. Run it through
**Actions → migrate → Run workflow**, not the Supabase SQL editor.

Pasting the `alter` into the dashboard fails: it rewrites every row plus
~195 MB of TOAST, which takes minutes, and the SQL editor's HTTP layer gives
up first with `Error: Load failed (api.supabase.com)`. That is a browser
timeout, not a database error — and the bad part is that it leaves no way to
tell whether the statement rolled back or is still running behind an
`ACCESS EXCLUSIVE` lock. A runner on a direct connection has no such
timeout, and the migration sets `statement_timeout = 0` in case the role
carries a non-zero default.

The migrate job's cap was raised from 15 to 60 minutes for the same reason: a
job cancelled mid-rewrite is the one outcome with no clear signal.

The rewrite needs temporary room for a second copy of the heap and TOAST,
which is why it comes after step 1.

Expected recall cost is small. Voyage's vectors are normalized and fp16 has
ample precision for cosine distance at this dimension; published comparisons
put the loss well under a point. This is an estimate, not a measurement —
worth re-running the eval set afterward to confirm.

### 3. Code changes for halfvec

Not optional — `halfvec <=> vector` has no operator, so retrieval breaks the
moment the column type changes. These ship together with the migration:

- `src/herald/schools_retrieval.py:103,106` — **done.** `%(qvec)s::vector` is
  now `%(qvec)s::halfvec(1024)`. This was the only distance query on the
  schools schema. `herald/db.py:253,259` also casts `::vector` but is the
  newspaper engine against a different database — deliberately untouched.
- `src/herald/schools_db.py:221` — the insert passes `list[float]`, which
  psycopg dumps as `double precision[]`, relying on pgvector's assignment
  cast to the column type. pgvector defines that cast for halfvec exactly as
  it does for vector, so this should keep working unchanged. Left alone
  rather than churned, since there is no database here to test it against —
  **the first real ingest is the check.**
- `src/herald/cluster.py:127,242` — `register_vector(conn)` registers halfvec
  too as of pgvector-python 0.3.0, and the pin is **0.4.2**. Embeddings load
  as fp16 arrays and are immediately widened by
  `np.array(..., dtype=np.float32)`, so no change needed.

### 4. Import the agendas

Before rebuilding any index, not after. Inserting 7,645 chunks into an
existing HNSW grows it messily; building once over the final corpus is
smaller and faster. With no index present the import is also quicker.

### 5. Score, then trim

`status` is only ever written by the scoring pass in `quality.py`, and
**it has never run on this corpus** — all 47,510 chunks are `active`, none
quarantined. That is not evidence the chunks are clean; it is the absence of
evidence either way.

Scoring is local — dictionary ratios against `wordlist.txt`, no API calls —
so it is cheap to run and worth doing on its own merits. Whatever it
quarantines is then safe to delete: quarantined chunks are excluded from
every index by the `where status = 'active'` predicate, so they earn nothing
while carrying a full embedding each.

Other trim candidates, once there is data to look at:

```sql
select doc_type, status, count(*) as chunks,
       pg_size_pretty((count(*) * 4100)::bigint)    as embeddings,
       pg_size_pretty(sum(length(content))::bigint) as content
from chunks
group by doc_type, status
order by count(*) desc;
```

- **`doc_type = 'other'`.** Everything the classifier could not place.
- Documents with `ingest_status = 'no_text'` that have chunks anyway.

Deleting a document cascades to its chunks (`on delete cascade`), so trimming
at the document level is safe and self-consistent.

Note that `delete` does **not** return space to the operating system — it
marks tuples dead, and `pg_database_size`, which is what the tier limit
measures, does not move. The space becomes reusable by later inserts, which
is worth something, but a reported reduction needs `vacuum full chunks`, and
that needs room for a second copy of the table.

### 6. Decide the index question

Everything above is settled. This step decides whether the result is
comfortable or tight.

| option | index | total | cost |
|---|---:|---:|---|
| HNSW on halfvec | ~138 MB | ~504 MB | none beyond step 3 |
| **no vector index** | 0 | **~366 MB** | queries take seconds, not ms |
| HNSW on binary-quantized vectors, rescored | ~20 MB | ~387 MB | two-stage query |

Recommended: **take no index for now and measure.** Brute-force cosine over
55,000 halfvec rows scans ~113 MB, on the order of a second or two. For a
research corpus queried interactively a handful of times a day that may be
entirely acceptable, and it is the only option that leaves real headroom.

If it proves too slow, the third row is pgvector's documented pattern for
this exact problem: index `binary_quantize(embedding)::bit(1024)` under
Hamming distance to get candidates fast, then rescore the top few hundred
against the real halfvec. 128 bytes per vector in the index instead of 2,050.
Worth writing against a proven need rather than a predicted one.

For reference, HNSW on halfvec would be:

```sql
create index chunks_hnsw_idx on chunks
  using hnsw (embedding halfvec_cosine_ops)
  where status = 'active';
```

## Expected end state

Estimates, not measurements:

| | now | after |
|---|---:|---:|
| `chunks_hnsw_idx` | 366 MB | 0, or ~20 MB quantized |
| chunks TOAST (embeddings) | ~195 MB | ~98 MB |
| everything else | ~230 MB | ~230 MB, less trimming |
| the 278 agendas | — | ~38 MB |
| **total** | **791 MB** | **~366 MB with no index** |

## Rejected alternative

A suggested shortcut was: drop the index, `delete from chunks where status =
'quarantined'`, then recreate the index as-is. Recorded because the reasoning
is instructive.

It fails on three counts:

1. The index is **partial** (`where status = 'active'`), so quarantined rows
   were never in it. Deleting them cannot make the rebuild smaller, though
   the sequence reads as though it does.
2. `delete` does not shrink `pg_database_size` without `vacuum full`.
3. Recreating the index as float32 gives back ~219 MB of the ~366 MB that
   dropping it saved.

With the measured `quarantined = 0`, step 2 of that sequence is a no-op and
the end state is **~644 MB** — still over the limit, with the work spent.
The estimate accompanying it (~300 MB) required roughly two-thirds of the
corpus being quarantined garbage, which no query had been run to establish.

## Still unknown

- The per-table breakdown. This query errored on the first attempt
  (`relname` is ambiguous — it exists in both `pg_class` and
  `pg_stat_user_tables`); the corrected form is:

  ```sql
  select c.relname,
         pg_size_pretty(pg_total_relation_size(c.oid))                       as total,
         pg_size_pretty(pg_relation_size(c.oid))                             as heap,
         pg_size_pretty(pg_indexes_size(c.oid))                              as indexes,
         pg_size_pretty(coalesce(pg_total_relation_size(c.reltoastrelid),0)) as toast,
         s.n_live_tup, s.n_dead_tup
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  left join pg_stat_user_tables s on s.relid = c.oid
  where n.nspname = 'public' and c.relkind = 'r'
  order by pg_total_relation_size(c.oid) desc
  limit 15;
  ```

  Without it, the ~230 MB "everything else" row above is a subtraction, not
  an observation. `documents` holds no full text, so it should be small; if
  it is not, something else is going on.

  In particular the `fts` column is `generated always as stored`, so there is
  a tsvector in every row that nothing has measured. A tsvector runs 30–40% of
  its source text, which puts it around 25–35 MB here — worth seeing, but not
  a major lever. (An earlier guess of 50–90 MB in conversation was too high.)

- How much scoring and trimming actually recover.
- Whether the project is genuinely on the free plan. It is writing happily at
  791 MB, which free-tier enforcement (read-only mode) would not allow, so
  either enforcement has not fired yet or the plan is not what we assume.
  Settings → Usage in the Supabase dashboard settles it.

## Settled since this file was opened

- pgvector is **0.8.2** — halfvec available, the plan is unblocked.
- **`quarantined = 0`**, all 47,510 chunks active. Scoring has never run.

## Correction to the earlier estimate

The 500 MB sizing that set the agenda snapshot's 24-month window used ~11 KB
per chunk. The observed figure is 16.6 KB (791 MB / 47,510), so that estimate
was optimistic by about half. The window choice still holds — two years is
what the newsletter goal needs regardless — but the headroom it was supposed
to leave did not exist.
