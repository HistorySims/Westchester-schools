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

### 2. Convert the embedding column to halfvec — DONE

Applied 2026-09-07, by accident of persistence: the statement was pasted into
the Supabase SQL editor, the editor timed out with `Error: Load failed
(api.supabase.com)`, and the backend went right on running it and committed.
`pg_attribute` now reports `halfvec` for `chunks.embedding`.

**The lesson worth keeping: a dashboard timeout does not abort the
statement.** The HTTP connection between the browser and Supabase's API drops;
the connection between the API and Postgres does not. Retrying a "failed"
statement can therefore mean running a second copy of one still in flight.
Check state before re-issuing anything long.

Migration 0007 is still worth running — its `alter` is guarded and will skip,
but it records itself in `schema_migrations` so the migration history matches
the database. Left unrun, it stays pending forever and `migrate status`
reports something false.

Run it through **Actions → migrate → Run workflow**, never the SQL editor.
The migration sets `statement_timeout = 0` in case the role carries a default,
and the migrate job's cap went from 15 to 60 minutes so a long rewrite is not
cancelled halfway.

#### What the change was

`halfvec(1024)` stores fp16 instead of fp32: 2,050 bytes per vector instead
of 4,100. Both the column and any future index halve. Available since
pgvector 0.7.0; Supabase is on **0.8.2**.

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

Everything above is settled. This is the last open question, and unlike the
earlier ones it is not answerable from a catalog query — it needs a real
search against the finished corpus.

Totals below use the measured 296 MB plus the projected import:

| option | index | total | cost |
|---|---:|---:|---|
| HNSW on halfvec | ~138 MB | ~480 MB | none beyond step 3 |
| **no vector index** | 0 | **~342 MB** | queries take seconds, not ms |
| HNSW on binary-quantized vectors, rescored | ~20 MB | ~362 MB | two-stage query |

All three now fit, which they did not when this file was opened. So the
decision is about latency, not space.

Recommended: **take no index for now and measure.** Brute-force cosine over
55,000 halfvec rows scans ~113 MB, on the order of a second or two. For a
research corpus queried interactively a handful of times a day that may be
entirely acceptable, and it costs nothing to find out — the index can be
added later without re-embedding anything.

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

## Measured after steps 1 and 2

**791 MB → 296 MB.** Measured 2026-09-07, not projected.

| table | total | heap | indexes | toast |
|---|---:|---:|---:|---:|
| `chunks` | 280 MB | 60 MB | 25 MB | 195 MB |
| `documents` | 4,456 kB | 2,904 kB | 1,512 kB | 8 kB |
| everything else | < 1.2 MB | | | |

`chunks` is 95% of the database. **The "everything else ≈ 230 MB" row in
earlier versions of this file was wrong** — it came from subtracting the HNSW
index from the total and attributing the remainder to other tables. There are
no other tables of consequence; that 230 MB was chunks' own heap and TOAST.

The 195 MB TOAST figure looks unchanged from before the conversion but is not
the same 195 MB. Embeddings in it went 195 MB → ~97 MB; what sits alongside
them is `content` plus the stored `fts` tsvector, ~98 MB. So content and fts
are now the largest thing in the database after the embeddings — the earlier
question about whether the generated tsvector was worth measuring is answered
yes.

Per chunk, all-in: 280 MB / 47,510 = **6.05 KB**, against 16.6 KB before.

## Where that leaves the tier

| | |
|---|---:|
| now | 296 MB |
| + 278 agendas (7,645 chunks × 6.05 KB) | ~342 MB |
| ...with no vector index | **~342 MB** |
| ...with a binary-quantized index | ~362 MB |
| ...with HNSW on halfvec | ~480 MB |

All three fit under 500 MB. **Trimming is therefore optional, not required.**
Scoring is still worth running — garbage OCR chunks pollute retrieval whether
or not they cost space — but it is no longer load-bearing for the limit.

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

- Whether semantic search is fast enough with no index. This is the only
  question left that changes what gets built, and it cannot be answered
  before the import — see step 6.

- How much scoring and trimming would recover. No longer urgent: at 296 MB
  every index option fits.

- Whether `salary_schedule`, `stipend_schedule` and `cluster_maps` hold
  anything. All three report `n_live_tup 0`, but `cluster_maps` shows 280 kB
  of TOAST, so it plainly has rows — `n_live_tup` is an ANALYZE estimate and
  reads 0 for a table never analyzed. A `count(*)` would settle it. Unrelated
  to space; noted because a genuinely empty `salary_schedule` would mean the
  structured extraction never landed, which matters for goal A.

- Whether the project is genuinely on the free plan. It was writing happily
  at 791 MB, which free-tier enforcement (read-only mode) would not allow, so
  either enforcement had not fired yet or the plan is not what we assume.
  Settings → Usage in the Supabase dashboard settles it. Moot for now, but
  worth knowing before the next growth spurt.

## Settled since this file was opened

- pgvector is **0.8.2** — halfvec available, the plan is unblocked.
- **`quarantined = 0`**, all 47,510 chunks active. Scoring has never run.
- **Step 1 done** — `chunks_hnsw_idx` dropped.
- **Step 2 done** — `chunks.embedding` is `halfvec`, confirmed via
  `pg_attribute`. Applied from the dashboard despite its timeout.
- **791 MB → 296 MB**, measured. Under the tier with 200 MB to spare, and
  every remaining option fits.
- The per-table breakdown is in, and it retired the "everything else"
  guesswork: `chunks` is 95% of the database.

Which leaves semantic search broken until the `::halfvec` cast in
`schools_retrieval.py` merges — there is no `halfvec <=> vector` operator.
Ingest is unaffected; it inserts through pgvector's assignment cast.

## Correction to the earlier estimate

The 500 MB sizing that set the agenda snapshot's 24-month window used ~11 KB
per chunk. The observed figure is 16.6 KB (791 MB / 47,510), so that estimate
was optimistic by about half. The window choice still holds — two years is
what the newsletter goal needs regardless — but the headroom it was supposed
to leave did not exist.
