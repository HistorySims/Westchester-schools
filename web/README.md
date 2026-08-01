# Westchester Schools — web app (v1)

The public front end: **Ask** (district-panel Q&A with citations) and
**full-text search** over the schools corpus. Next.js on Vercel, reading
Supabase; the heavy batch work (ingest, cluster) stays in GitHub Actions. See
[`../docs/ROADMAP.md`](../docs/ROADMAP.md) for where this is headed (the topic
map is v2).

## Architecture

- **`/api/ask`** — streams a district-by-district answer. Retrieval is the TS
  port of `herald.schools_retrieval`: per-district semantic + FTS legs (Postgres
  RPCs), RRF fused within each district, one pooled Voyage rerank, per-document
  cap; then `claude-sonnet-5` synthesis with `[N]` citations and honest
  absence-reporting (`src/lib/schools-retrieval.ts`, `schools-synth.ts`).
- **`/api/search`** — global Postgres full-text search (the "every mention of X"
  box). Cheap; runs entirely in the database.
- **`/api/districts`** — the district roster for the filter chips.
- **`/explore`** — the topic map. Reuses the standalone canvas renderer
  (`public/cluster_map.html`) in an iframe, fed the latest snapshot from
  **`/api/map`** (the `cluster_maps` table) via `postMessage`. Populate it with
  `herald-cluster run --publish`.

Retrieval ranks per district in SQL, so one verbose district can't crowd out the
panel and districts with no evidence are reported — the same design as the CLI.

## Setup

1. **Run the RPC migration** against the schools database (once):

   ```bash
   psql "$SUPABASE_DB_URL" -f supabase/migrations/20260724_schools_rpcs.sql
   ```

   It creates `match_school_chunks_semantic`, `match_school_chunks_fts`, and
   `search_school_chunks`. The `chunks` FTS index and HNSW index already exist
   from `db/migrations/0001_schools_init.sql`. For the topic map, also run
   `20260725_cluster_maps.sql` (creates the `cluster_maps` table `/explore`
   reads).

2. **Set environment variables** (locally in `.env.local`, and in the Vercel
   project settings):

   | var | what |
   |---|---|
   | `SUPABASE_URL` | project URL (`https://<ref>.supabase.co`) |
   | `SUPABASE_SERVICE_ROLE_KEY` | service-role key (server-side only; never shipped to the client) |
   | `VOYAGE_API_KEY` | embeddings + rerank |
   | `ANTHROPIC_API_KEY` | synthesis (`claude-sonnet-5`) |

3. **Deploy**: connect this repo to Vercel with the **root directory set to
   `web/`**. Vercel auto-deploys on push; the config is in `vercel.json`.

## Local dev

```bash
npm install
npm run dev     # http://localhost:3000
npm run build   # production build (what Vercel runs)
```

## Not yet wired

The monthly **brief** and **trajectory** views (see `../docs/ROADMAP.md`).
`package.json` still lists `deck.gl`/`luma.gl` from the newspaper app — unused
(our map is the vanilla-canvas renderer), kept out of harm's way; they
tree-shake out of the client bundle.
