# Herald — Plan

> **Working title:** Herald — semantic research over historical American newspapers.

> **STATUS (June 2026):** Phases 1 and 2 are shipped — the CLI, ingest, hybrid retrieval, web UI with citations, and conversation history all exist and work. Phase 3 (auth, Stripe, vision-based 5¢ Cleanup) is **deferred indefinitely**: the Library of Congress now sits behind Cloudflare bot protection that blocks our cloud-IP fetches for both ingest *and* image retrieval. Until LOC allowlists us or we find another path to the page images, the cleanup feature can't run.
>
> A major addition not in this plan — **the `/explore` page** with hierarchical clustering, burstiness ranking, and cluster stories — grew organically post-Phase-2 and is now the heart of the product. See `docs/CLUSTERING_PLAN.md` for that design and `docs/STATUS.md` for the current product description.

---

## 1. Context

Historical newspapers are the closest thing we have to a continuous, granular ground-truth record of American life — and Chronicling America (Library of Congress) has digitized millions of pages with OCR text and high-resolution page images. But the standard interface is keyword search over noisy 1840s OCR, which buries connections under page-flips. The product hypothesis: **semantic search + LLM synthesis + image-first citations** turns this corpus from an archive into a research instrument.

The demo corpus is **two papers from 1842–1846**, both fully digitized on Chronicling America:

- **The New-York Daily Tribune** (Horace Greeley), New York, N.Y., daily from 1842-04-22 — Whig-reform, sympathetic to tenant grievances, nationally circulating.
- **The Albany Evening Journal** (Thurlow Weed), Albany, N.Y., daily — Whig establishment, physically adjacent to the conflict, the capital's political organ.

This pairing was chosen because the **Anti-Rent Wars** (the 1840s upstate tenant rebellion that ended manorial land tenure in New York) get rich coverage in both papers from two distinct vantages: Greeley reporting from New York City with a reform editorial lean, Weed reporting from Albany right next to the action with an establishment Whig register. That contrast is itself a feature — semantic search across two papers makes "how did urban vs. Albany journalism frame the same event?" a tractable question, which is exactly the kind of connection the standard keyword-search interface buries.

The conflict is regionally significant, nationally obscure, and visually dramatic (sheriffs ambushed by men in calico disguises): a perfect demo for historians, archive boards, and educators, with the emotional pitch "what's *our* town's version of this story, buried in our local paper?"

The user-facing artifact is always the **original page image**, never raw OCR. OCR is the retrieval index; the page is the source of truth. Every synthesized claim links back to a specific chunk on a specific page image, and citation hallucination is a fatal flaw.

---

## 2. Corpus verification

**Verification was done in two passes:** LCCN/digitization confirmation, then a sanity check that Anti-Rent content actually appears in the OCR (because LOC digitization alone doesn't guarantee topical coverage). The original product brief named the New York Herald (`sn83030313`) 1840–1845; verification (below) found Anti-Rent coverage in the Herald appears thin, so the corpus was switched per the user-approved fallback order.

### Final corpus (Phase 1)

| Paper | LCCN | Place | Date range | Editor / lean | Why it's in |
|---|---|---|---|---|---|
| New-York Daily Tribune | `sn83030213` | New York, N.Y. | 1842-04-22 → 1846-12-31 | Horace Greeley (Whig-reform) | National-circulation paper with active editorial engagement on Anti-Rent; Greeley sympathetic to tenants |
| Albany Evening Journal | `sn83030911` | Albany, N.Y. | 1842-01 → 1846-12-31 | Thurlow Weed (Whig establishment) | In the capital, next to the conflict; daily political organ; different Whig register than the Tribune |

Both are confirmed digitized on Chronicling America with OCR `.txt`, JP2 page images, and PDF derivatives served per page.

### Why not the New York Herald (the original pick)

Targeted Google `site:chroniclingamerica.loc.gov` searches against `sn83030313` for Anti-Rent terminology:

- `"anti-rent"` → **0 hits** in the LCCN
- `"Calico Indians"` → **0 hits** in the LCCN
- `"Van Rensselaer" rent tenant` → **0 hits** in the LCCN
- `Helderberg` → some 1842 hits (place-name only, may not be rent-related)

Google's index of LOC OCR is incomplete, so this isn't conclusive. But it aligns with what we know about Bennett's *Herald*: it was a sensationalist, NYC-centric penny press focused on national news and crime, not state-political coverage. It would have run the dramatic 1845 events (Steele's killing) but skipped the day-to-day tenant politics that make a corpus *interesting* to a semantic search tool. Tribune and Evening Journal both carried that detail, so they're the better fit.

### Predecessor / adjacent titles, not in scope for Phase 1

- `sn83030311` — "The Herald" (New York, N.Y.), 1835–1837
- `sn83030312` — "Morning Herald" (New York, N.Y.), 1837–1840
- `sn83030212` — "New-York Tribune" (weekly precursor), 1841–1842
- `sn83030908` — "Albany Argus" (Democratic), 1828–1856 — natural future add if we want a non-Whig voice
- `sn85038675` — "The Albany Freeholder" (1845–1854) — the Anti-Rent movement's *own* paper. Starts too late for a full arc but is a strong candidate for a later corpus expansion.

### Anti-Rent arc covered by the 1842–1846 window

| Event | Date | In-corpus? |
|---|---|---|
| Initial Helderberg meetings, sheriff posse repulsed | 1839 (pre-corpus) | Background only |
| Stephen Van Rensselaer III dies, heirs demand back rent | Jan 1839 (pre-corpus) | Background only |
| Tenant organizing spreads to Columbia/Delaware/Schoharie counties | 1842–1844 | ✅ |
| "Calico Indians" secret order forms | May 1844 | ✅ |
| Governor Bouck calls up militia to disband Calico Indians | Jan 1845 | ✅ |
| Sheriff Osman Steele killed at Moses Earle's farm, Andes | Aug 7, 1845 | ✅ |
| Anti-disguise law passes, Governor Wright crackdown | 1845 | ✅ |
| Trials of Calico Indian leaders ("Big Thunder" / Smith Boughton et al.) under Judge Amasa J. Parker | 1845 | ✅ |
| Governor John Young pardons Boughton and commutes others | 1846–1847 | ✅ (early aftermath) |

---

## 3. Architecture overview

```
                                           ┌──────────────────────────────┐
                                           │  Chronicling America (LOC)   │
                                           │  - issues JSON               │
                                           │  - page OCR (.txt)           │
                                           │  - page images (JP2 / JPEG)  │
                                           └───────────────┬──────────────┘
                                                           │
                                            (Phase 1 batch ingest worker)
                                                           ▼
        ┌─────────────────────────────────────────────────────────────────────────┐
        │                            Ingestion pipeline                            │
        │                                                                          │
        │  for each paper in (sn83030213, sn83030911):                             │
        │    enumerate issues → download page OCR + image URLs → normalize text   │
        │       │                                                       │          │
        │       └──→ store papers/issues/pages rows ◄────────────────────┘          │
        │                                                                          │
        │                            chunk page text (400w / 50w overlap)          │
        │                                          │                               │
        │                                          ▼                               │
        │                            Voyage AI embeddings (batched)                │
        │                                          │                               │
        │                                          ▼                               │
        │                            insert chunks (text + vector + tsvector)      │
        └─────────────────────────────────────────────────────────────────────────┘
                                                           │
                                                           ▼
        ┌─────────────────────────────────────────────────────────────────────────┐
        │                          Supabase (Postgres + pgvector)                  │
        │   papers · issues · pages · chunks · users · credits · reocr_jobs        │
        │   indexes: HNSW (vector) + GIN (tsvector) + btree (date, page)           │
        └─────────────────────────────────────────────────────────────────────────┘
                                                           │
                                                           ▼
                            ┌──────────────────────────────────────────┐
                            │           Retrieval pipeline              │
                            │                                           │
                            │ user query (+ optional paper_id filter)   │
                            │   ├─ embed query (Voyage)                 │
                            │   ├─ semantic top-k (HNSW)                │
                            │   ├─ FTS top-k (GIN, tsvector)            │
                            │   ├─ RRF merge (k=60)                     │
                            │   ├─ Voyage rerank-2.5 (cross-encoder)    │
                            │   └─ MMR diversity step (synthesis Qs)    │
                            └──────────────────┬───────────────────────┘
                                               ▼
                            ┌──────────────────────────────────────────┐
                            │          Synthesis (Claude Sonnet 4.6)    │
                            │  - chunks tagged with paper + date        │
                            │  - cite-or-refuse system prompt           │
                            │  - returns claims + citation chunk_ids    │
                            └──────────────────┬───────────────────────┘
                                               ▼
                            ┌──────────────────────────────────────────┐
                            │       Phase 1: CLI prints answer +        │
                            │       citations (paper + date + page).    │
                            │       Phase 2: React split-screen UI.     │
                            │       Phase 3: "5¢ Cleanup" re-OCR loop.  │
                            └──────────────────────────────────────────┘
```

The pipeline is **batch-ingestion + online-retrieval**. Re-OCR (Phase 3) is a separate, user-triggered write path that re-runs the chunk→embed→store leg for a single page.

---

## 4. Data flow, end to end

### 4.1 Ingestion (Phase 1, run once per paper over 1842–1846)

1. **Enumerate.** Use the Chronicling America batch/JSON API to list every issue of the LCCN with `date_issued` in window. For each issue, list its pages.
2. **Fetch.** For each page, pull:
   - OCR plain text (`.../ocr.txt`)
   - Page image URLs (we store JP2 + a JPEG-derivative URL; the UI uses JPEG/IIIF)
   - Metadata (page sequence number, edition, issue date)
3. **Normalize.** Light cleanup only — Unicode normalize, collapse runs of whitespace, drop control chars, preserve linebreaks (they hint at column boundaries for downstream improvements). **No de-hyphenation, no column reflow, no article boundary detection in Phase 1.**
4. **Persist** `papers / issues / pages` rows.
5. **Chunk.** 400-word windows with 50-word overlap (see §6).
6. **Embed.** Batch chunks (128 per request) to Voyage AI `voyage-3.5` at 1024 dims (see §7).
7. **Write** chunks with `embedding` vector and a Postgres `tsvector` column generated from `content`. HNSW + GIN indexes maintain themselves on insert.
8. **Checkpoint.** Resume safely on crash: ingestion is idempotent keyed on `(lccn, date_issued, edition, sequence)` for pages and `(page_id, ocr_version, chunk_index)` for chunks.

Ingestion runs **once per paper** but writes into a single shared schema. Tribune and Evening Journal pages share index space; retrieval can filter by `paper_id` or merge across both.

### 4.2 Retrieval (Phase 1 CLI; Phase 2 web)

1. Embed the user query with Voyage (same model as corpus).
2. **Semantic top-k** (`k_sem = 50`) via HNSW cosine. Apply optional `paper_id` and `date` filters in the `WHERE` clause.
3. **FTS top-k** (`k_fts = 50`) via `tsvector @@ websearch_to_tsquery('english', $q)`, ranked by `ts_rank_cd`.
4. **RRF merge** into a single ranked list of ~80 unique chunks (`k = 60`).
5. **Cross-encoder rerank** top 80 → top 20 via Voyage `rerank-2.5`.
6. **MMR diversity** (λ=0.5) over the top 20 → final 8–12 chunks for synthesis. MMR is applied conditionally: synthesis-style queries ("how does each paper characterize X across this period?") benefit; narrow factual queries skip it.
7. Pass chunks to the synthesizer with stable IDs and full citation metadata (paper title + date + page sequence + image URL).

### 4.3 Synthesis & display

The LLM gets a numbered chunk dossier and a strict "cite-or-refuse" system prompt (§9). It returns an answer with inline citation markers (`[3]`, `[7]`) that map back to chunks the retrieval layer chose. The CLI (Phase 1) prints answer + numbered source list with paper, date, page, sequence, and the LOC page image URL. The Phase 2 UI clicks a citation → opens that page image in the right-hand viewer.

---

## 5. Supabase schema

> Conventions: `id uuid default gen_random_uuid() primary key`. Timestamps `created_at`/`updated_at` default `now()`. All FKs `on delete cascade` where the parent's deletion implies the child's irrelevance. SQL is canonical-ish but will be a real migration file in Phase 1.

```sql
-- ============================================================
-- papers: one row per Chronicling America LCCN we ingest
-- ============================================================
create table papers (
  id            uuid primary key default gen_random_uuid(),
  lccn          text unique not null,           -- 'sn83030213' | 'sn83030911' | ...
  title         text not null,                  -- 'New-York Daily Tribune'
  place         text,                            -- 'New York, N.Y.'
  start_year    int,
  end_year      int,
  created_at    timestamptz not null default now()
);

-- ============================================================
-- issues: one row per (paper, date, edition)
-- ============================================================
create table issues (
  id          uuid primary key default gen_random_uuid(),
  paper_id    uuid not null references papers(id) on delete cascade,
  date_issued date not null,
  edition     int  not null default 1,
  loc_url     text not null,                    -- canonical LOC issue URL
  created_at  timestamptz not null default now(),
  unique (paper_id, date_issued, edition)
);
create index issues_date_idx       on issues (date_issued);
create index issues_paper_date_idx on issues (paper_id, date_issued);

-- ============================================================
-- pages: one row per page image / OCR text
-- ============================================================
create table pages (
  id              uuid primary key default gen_random_uuid(),
  issue_id        uuid not null references issues(id) on delete cascade,
  sequence        int  not null,                -- 1..N within issue
  image_url       text not null,                -- JPEG-derivative for UI
  jp2_url         text,                          -- high-res master
  pdf_url         text,
  ocr_text        text,                          -- raw OCR (current best)
  ocr_version     int  not null default 1,      -- bumped on re-OCR
  ocr_source      text not null default 'loc',  -- 'loc' | 'claude-vision' | ...
  cleaned_at      timestamptz,                   -- non-null iff re-OCR'd
  cleaned_by_user uuid references users(id),    -- optional attribution
  reocr_status    text not null default 'original', -- 'original'|'pending'|'cleaned'|'failed'
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (issue_id, sequence)
);
create index pages_reocr_idx on pages (reocr_status) where reocr_status <> 'original';

-- ============================================================
-- chunks: one row per retrieval unit
-- ============================================================
create table chunks (
  id                uuid primary key default gen_random_uuid(),
  page_id           uuid not null references pages(id) on delete cascade,
  ocr_version       int  not null,              -- matches the page version this chunk was built from
  chunk_index       int  not null,              -- 0..N within page+version
  content           text not null,              -- the actual chunked text
  word_start        int  not null,              -- approximate token/word offset on page
  word_end          int  not null,
  embedding         vector(1024),               -- Voyage 3.5, default dim
  fts               tsvector generated always as (to_tsvector('english', content)) stored,
  is_current        boolean not null default true, -- false when superseded by newer ocr_version
  created_at        timestamptz not null default now(),
  unique (page_id, ocr_version, chunk_index)
);
create index chunks_hnsw_idx on chunks
  using hnsw (embedding vector_cosine_ops)
  where is_current = true;
create index chunks_fts_idx  on chunks using gin (fts) where is_current = true;
create index chunks_page_idx on chunks (page_id);

-- ============================================================
-- users: Phase 3-active, schema lives in Phase 1
-- ============================================================
create table users (
  id                uuid primary key default gen_random_uuid(),
  email             text unique,                -- linked to Supabase auth.users in Phase 3
  display_name      text,
  credits_remaining int  not null default 0,
  created_at        timestamptz not null default now()
);

-- ============================================================
-- credit_ledger: append-only history of credit grants/spends
-- ============================================================
create table credit_ledger (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  delta       int  not null,                    -- +N grant, -1 spend
  reason      text not null,                    -- 'signup_bonus'|'purchase'|'reocr_page'|'refund'
  page_id     uuid references pages(id),        -- non-null on reocr_page spends
  created_at  timestamptz not null default now()
);
create index credit_ledger_user_idx on credit_ledger (user_id, created_at desc);

-- ============================================================
-- reocr_jobs: tracks Phase 3 "5¢ Cleanup" runs
-- ============================================================
create table reocr_jobs (
  id              uuid primary key default gen_random_uuid(),
  page_id         uuid not null references pages(id) on delete cascade,
  requested_by    uuid not null references users(id),
  status          text not null default 'queued', -- queued|running|succeeded|failed
  model           text,                            -- e.g. 'claude-sonnet-4-6-vision'
  prev_ocr_version int  not null,
  new_ocr_version  int,
  error           text,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz not null default now()
);
-- enforces: at most one in-flight cleanup per (page, version)
create unique index reocr_jobs_inflight_idx on reocr_jobs (page_id, prev_ocr_version)
  where status in ('queued','running');
```

### Index rationale

- **HNSW on `chunks.embedding`** — Supabase docs' default for ANN; better recall than IVFFlat at production scale, no `nprobe` tuning. Partial index on `is_current = true` keeps the index small after re-OCR.
- **GIN on `chunks.fts`** — generated-always `tsvector` keeps FTS in sync without triggers.
- **Partial indexes on `is_current`** — when a page is re-OCR'd we leave old chunks in place (cited PRs may resolve to them) but flip `is_current=false` so they drop out of retrieval indexes.
- **Compound index `issues (paper_id, date_issued)`** — common filter pattern for paper-scoped or date-windowed queries.
- **Partial-unique on `reocr_jobs (page_id, prev_ocr_version) WHERE status IN ('queued','running')`** — prevents two users from simultaneously paying to clean the same page version.

---

## 6. Chunking strategy

**Decision: 400 words / 50-word overlap, fixed window, on per-page text.** Confirmed as proposed; this lands in the modern RAG sweet spot (~500–700 tokens, 10–15% overlap).

- **Why not article-boundary detection?** 1840s OCR over multi-column broadsheets is too noisy: no reliable visual signal in plain `.txt`, no preserved column structure, no XML article tags from LOC. Heuristics give net-negative quality at this stage and would blow the MVP timeline. Embeddings are robust to articles bleeding across chunks.
- **Why not semantic / recursive chunking?** Same reason — they need cleaner inputs than the LOC OCR provides. Worth revisiting *after* the 5¢ Cleanup path produces denoised text.
- **Sentence-respecting fallback.** Within the fixed-size budget we snap window boundaries to the nearest sentence end (or whitespace if none exists in ±40 words). Prevents amputating words at chunk edges.
- **Embed at chunk level, not page level.** Page-level embeddings drown a single relevant paragraph in 5,000 words of unrelated columns.
- **One page → ~10–15 chunks** at ~5,000 words/page (1840s broadsheet density). Across the combined corpus this is roughly **130k–180k chunks**.
- **Chunk identity.** A chunk is `(page_id, ocr_version, chunk_index)`. The chunk UUID is stable; on re-OCR we mint *new* chunks rather than rewriting old ones (see §10), and old chunks are kept with `is_current=false` so prior citations still resolve.

---

## 7. Embedding model

**Recommended: Voyage AI `voyage-3.5`** (drop-in replacement for the spec'd `voyage-3`; same price, strictly better retrieval quality per Voyage's published benchmarks).

| Field | Value |
|---|---|
| Model | `voyage-3.5` |
| Dimensions | **1024** (default; Matryoshka — can re-quantize to 512/256 later without re-embedding) |
| Price | **$0.06 / 1M tokens** (input only) |
| Quantization | float32 at rest in pgvector; can move to int8 later if disk/RAM bites |
| Batching | 128 chunks/request, retry-on-429 with backoff, persist after each batch |
| Throughput target | ~10–20k chunks/min after warmup |
| Multilingual | English-only is fine for this corpus |

Use the **same model & dimensionality at query time**. If we ever change embedding models, we re-embed the whole corpus — never mix versions in one HNSW index.

---

## 8. Retrieval pipeline

### Hybrid retrieval

```
            ┌──────────────────┐         ┌──────────────────┐
 query  →   │ embed (voyage)   │         │ websearch_to_    │
            │ → HNSW top 50    │         │ tsquery → GIN    │
            └────────┬─────────┘         │ top 50 by        │
                     │                   │ ts_rank_cd       │
                     │                   └────────┬─────────┘
                     └──────────┬─────────────────┘
                                ▼
                       RRF merge (k=60)
                                │
                                ▼
                  Voyage rerank-2.5 → top 20
                                │
                                ▼
                  Optional MMR diversity → top 8–12
                                │
                                ▼
                         to synthesis
```

- **RRF k=60** — Supabase's documented default and a well-validated starting point. Tune against the validation set in §12 only after measurement.
- **Reranker: Voyage `rerank-2.5`** — 32K context, instruction-following, $0.05/1M tokens, **first 200M tokens free per account**, which more than covers Phase 1 evaluation.
- **MMR step** — applied for breadth-y queries (heuristic: query contains "how", "what kinds of", "across", "compare", or no proper noun, OR explicitly asks about both papers). λ=0.5. Skipped for narrow factual queries to avoid bleeding away the best hit.
- **Filters** — accept optional `paper_id`, `date_from`, `date_to`. Filters apply *before* HNSW search via `WHERE` clause on the partial-indexed query, so a Tribune-only query stays as fast as a full-corpus query.
- **Cross-paper queries are first-class.** A query that asks "how do the Tribune and Evening Journal differ on…" should retrieve from both; the MMR step (with `paper_id` as a soft diversity term) ensures the synthesis context has chunks from both papers when both are relevant.

### Failure modes the pipeline must handle

- Empty FTS result (rare; common-word queries → still has hits). Degrade to semantic-only.
- Empty semantic result (very rare). Degrade to FTS-only.
- Both empty → return "I couldn't find this in the corpus" without calling the LLM.

---

## 9. Synthesis prompts

### System prompt (sketch)

> You are a research assistant grounded in a specific newspaper corpus. You will be given a numbered list of source passages (chunks) from two New York newspapers — the *New-York Daily Tribune* (Horace Greeley) and the *Albany Evening Journal* (Thurlow Weed) — between 1842 and 1846. Each chunk has an ID, a paper name, a date, and a page reference. Answer the user's question using **only** these passages.
>
> **Citation rule.** Every factual claim must be followed by one or more citation markers in the form `[N]`, where `N` is the chunk's number in the source list. Do not cite chunks you did not use. Do not invent chunk numbers. If the passages do not contain enough evidence to answer, say so plainly and stop — do not pad with general knowledge.
>
> **Paper-aware attribution.** When a claim derives from a specific paper, name the paper in your prose ("the Tribune reports…", "the Evening Journal frames it as…"). When the two papers disagree or use different language about the same event, surface the contrast — that contrast is often the point of the question.
>
> **Tone.** Write like a careful historian briefing a colleague: precise, plain, neither breezy nor stuffy. Quote the papers sparingly and only when their exact wording is the point. Do not modernize 19th-century terminology silently; if you use a period term like "Calico Indians" or "patroon," let the chunks do the explaining.
>
> **Refusal floor.** If fewer than two chunks address the question, default to "The corpus does not have enough to support a confident answer — here is what little it does say: …" Better to be small than wrong.

### User-turn template

```
QUESTION: <user query>

SOURCES:
[1] New-York Daily Tribune, 1844-08-12, p.2, seq 2, chunk a3f… —
    <chunk text>

[2] Albany Evening Journal, 1845-08-09, p.1, seq 1, chunk 9c4… —
    <chunk text>
...
```

### Output contract

A short prose answer with inline `[N]` markers. The application layer re-maps `N` → chunk UUID → page metadata + image URL when rendering. No JSON output mode in Phase 1 — strict markers are simpler and easier to QA.

### Anti-hallucinated-citation safeguard

After the model responds, the application **validates every `[N]`** against the IDs it sent in. Any unknown marker fails the response, retried once with a stronger reminder, then surfaced to the user as a system error rather than a wrong answer.

**Synthesis model: Claude Sonnet 4.6** for Phase 1. Opus 4.7 is overkill for grounded summarization at this corpus size, and Sonnet 4.6's pricing ($3/$15 per 1M tokens) makes per-query cost trivial (§11).

---

## 10. "5¢ Cleanup" — re-OCR mechanism

> Design only in Phase 1. **No vision-API calls are written until Phase 3.**

### User flow (Phase 3)

1. User reads a synthesized answer, clicks a citation, sees the page image (with the cited chunk visually highlighted).
2. Above the image: "OCR is messy. Clean this page for 1 credit (≈5¢)."
3. User clicks. We deduct one credit *immediately* in a transaction, enqueue a `reocr_jobs` row, return a job ID.
4. A worker runs vision OCR on the page JP2, parses the response into chunks, re-embeds, and atomically swaps the page's chunks (see below).
5. On success: the page now serves cleaned chunks; the credit-purchasing user is optionally credited in a "cleaned by" badge.
6. On failure: refund the credit (`credit_ledger` row, `delta = +1`, `reason = 'refund'`), mark the job `failed`.

### Backend (write path)

```
spend_credit_and_enqueue(user_id, page_id):
  BEGIN;
    SELECT credits_remaining FROM users WHERE id = user_id FOR UPDATE;
    IF credits_remaining < 1 THEN abort;
    UPDATE users SET credits_remaining = credits_remaining - 1 WHERE id = user_id;
    INSERT INTO credit_ledger (user_id, delta, reason, page_id) VALUES (user_id, -1, 'reocr_page', page_id);
    INSERT INTO reocr_jobs (page_id, requested_by, prev_ocr_version)
      VALUES (page_id, user_id, (SELECT ocr_version FROM pages WHERE id = page_id))
      ON CONFLICT (page_id, prev_ocr_version) WHERE status IN ('queued','running') DO NOTHING
      RETURNING id;
    -- if no row returned (conflict): another job already in flight → refund and return existing job
  COMMIT;
```

### Worker (vision call → re-chunk → re-embed → atomic swap)

```
run_reocr(job_id):
  mark job 'running', stamp started_at
  fetch page JP2 (or JPEG derivative) from LOC
  call vision model (Claude Sonnet 4.6 vision, or whatever wins the build-time bake-off)
    prompt: "Transcribe every word on this newspaper page, preserving paragraph breaks and
             column order top-to-bottom, left-to-right. Output plain text only."
  parse response → cleaned_text
  chunks_new = chunk(cleaned_text, 400/50)
  embeddings = voyage.embed([c.content for c in chunks_new])
  BEGIN;
    new_version = pages.ocr_version + 1
    UPDATE pages
       SET ocr_text=$cleaned, ocr_version=$new_version, ocr_source='claude-sonnet-4-6-vision',
           reocr_status='cleaned', cleaned_at=now(), cleaned_by_user=$requested_by,
           updated_at=now()
     WHERE id=$page_id;
    UPDATE chunks SET is_current=false WHERE page_id=$page_id AND is_current=true;
    INSERT INTO chunks (page_id, ocr_version, chunk_index, content, word_start, word_end,
                        embedding, is_current)
      VALUES (...), (...), ...;
    UPDATE reocr_jobs SET status='succeeded', new_ocr_version=$new_version, finished_at=now()
     WHERE id=$job_id;
  COMMIT;
```

### Citation stability across re-OCR

The challenge: a synthesized answer may have been cached/linked with chunk UUIDs that are now `is_current=false`. We handle this by:

1. **Never deleting old chunks.** They keep their UUIDs and their `page_id`. Lookups by chunk UUID still work; they just aren't returned by retrieval anymore.
2. **Citation resolver.** When the UI follows a citation, it goes `chunk_id → page_id → page image URL`. The image is the citation's truth, not the chunk text. The chunk text appears as a "preview snippet" near the citation; if the chunk is stale we badge it "from an earlier transcription of this page."
3. **No chunk-ID rewriting.** Cheaper, safer, and avoids the impossible "match new chunks to old chunks by content" problem.

### Concurrency / idempotency

- The partial-unique index on `reocr_jobs (page_id, prev_ocr_version) WHERE status IN ('queued','running')` blocks duplicate in-flight jobs at the database level.
- A second user clicking "Clean this page" while a job is queued/running gets a friendly "already in progress — you'll see results in a minute" response with no credit charged.
- If the worker dies mid-job, a janitor cron sweeps `running` jobs older than N minutes back to `queued` (or `failed` if retry-counted out).

---

## 11. Cost model

### Per-query (retrieval + synthesis)

| Component | Tokens | Unit price | Per query |
|---|---|---|---|
| Embed query | ~30 tokens | $0.06/1M | $0.000002 |
| Voyage rerank (q × 80 docs ≈ 35k tokens) | 35k | $0.05/1M (free first 200M) | $0 (effective) |
| Synthesis input (~12 chunks × 600 tok + system + history ≈ 15k) | 15k | $3/1M (Sonnet 4.6) | $0.045 |
| Synthesis output | ~800 | $15/1M | $0.012 |
| **Per query (no caching)** | | | **~$0.06** |
| Per query with prompt caching on system prompt | | | **~$0.02–0.03** |

### Per "5¢ Cleanup" page (Phase 3)

| Component | Cost |
|---|---|
| Vision input (~1,568 image tokens) | ~$0.005 |
| Vision output (~3,000 tokens, full page transcription) | ~$0.045 |
| Re-embed page (~5,000 words ≈ 7,500 tokens) | ~$0.0005 |
| Postgres write (negligible) | ~$0 |
| **Total per page** | **~$0.050** |

The 5¢ price tag is not a marketing round-number — it is the actual API cost. A credit sold at 5¢ breaks even; sold at 10¢ funds infrastructure and future corpora; sold at 25¢ in 10-credit packs funds margin.

### One-time corpus ingest cost (Tribune + Albany Evening Journal, 1842–1846)

Assumptions:

| Paper | Issues (≈) | Avg pages/issue | Pages (≈) | Words (≈) | Tokens (≈) |
|---|---|---|---|---|---|
| New-York Daily Tribune (1842-04 → 1846-12) | ~1,490 | 4–5 | ~7,000 | ~35M | ~50M |
| Albany Evening Journal (1842 → 1846) | ~1,560 | 4 | ~6,200 | ~30M | ~45M |
| **Combined** | **~3,050** | | **~13,000** | **~65M** | **~95M** |

| Component | Cost |
|---|---|
| Voyage `voyage-3.5` embeddings (~95M tokens) | **~$5.70** |
| Egress / LOC fetch | $0 (LOC is free, public-domain) |
| Supabase storage (~6 GB chunks + vectors + tsvectors) | low-double-digits/month |
| **One-time ingest** | **< $10** |

The corpus is cheap. Product economics live in **per-query synthesis** and **per-page Cleanup**, both modest.

---

## 12. Validation questions (Phase 1 acceptance tests)

Phase 1 is **not done** until the CLI produces well-cited, factually defensible answers to all of the following. Each is hand-graded against the page images; "well-cited" means at least 2 distinct citations with the right paper/date/page that actually contain the claim.

1. **"Find references to the Helderberg disturbances and the Calico Indians, 1842–1846."** — Basic semantic recall on the corpus's signature topic; should pull from both papers.
2. **"How does the Tribune characterize tenants versus landlords, compared to the Evening Journal? Quote phrasings."** — Cross-paper synthesis; MMR + paper-aware retrieval must both work; exact-phrase recall via FTS leg.
3. **"Trace coverage of Stephen Van Rensselaer III's death and its aftermath as the corpus discusses it."** — Tests temporal reasoning across multiple issues. Van Rensselaer III died Jan 1839 (pre-corpus), so the corpus discusses the rent claims his heirs pressed afterward.
4. **"What language do the papers use for tenant violence vs. landlord property claims? Quote specific phrasings."** — Tests close-reading via exact-quote retrieval; FTS leg must carry weight; cross-paper contrast.
5. **"Identify the named anti-rent leaders who appear repeatedly, and describe how each is characterized."** — Entity-level recall (Smith Boughton / "Big Thunder", Moses Earle, Osman Steele, Silas Wright, John Young, Stephen Van Rensselaer IV).
6. **"How do the papers report the killing of Sheriff Osman Steele at Andes in August 1845?"** — Precise event-level retrieval; date-filtered slice should work; comparing accounts across papers is the demo moment.
7. **"How is Governor Silas Wright's 1845 anti-disguise law and crackdown covered? Does the Tribune lean differently from the Evening Journal?"** — Political/editorial-stance extraction across papers.
8. **"Do the Anti-Rent Wars share newspaper real estate with national stories like the 1844 election or Texas annexation? What gets bumped, what gets prominence?"** — Cross-topic synthesis and page-prominence reasoning.
9. **"Refuse gracefully: what does the corpus say about the discovery of helium?"** — Negative-case test. Helium is post-corpus; the model must refuse, not confabulate. The citation-validator from §9 should also flush hallucinated marker IDs.
10. **"Find any reference to a specific upstate town — e.g. Berne, Rensselaerville, Andes, or Delhi — and summarize what the papers say happened there."** — Tests the demo's emotional pitch: "what's our town's version of this story?"

A failure on any of #1–#8 or #10 blocks Phase 2; #9 is a hard blocker for any release.

---

## 13. Phased build plan

### Phase 1 — Data, retrieval, CLI (this is what we build first)

- Repo scaffolding (Python; `uv` or Poetry; `ruff` + `pyright`; `pytest`).
- Supabase project provisioned (free tier is fine for the demo) with the **full schema from §5**, including `users`, `credit_ledger`, and `reocr_jobs` — *defined but unused*.
- Ingestion CLI: `herald ingest --lccn sn83030213 --from 1842-04-22 --to 1846-12-31` (and again for `sn83030911`). Resumable, idempotent, batched, with progress reporting.
- Chunker (400/50, sentence-snapping).
- Voyage embedding client with batch + retry.
- Retrieval module: hybrid HNSW + FTS + RRF + rerank + optional MMR; supports `--paper` and `--date-from/--date-to` filters.
- Synthesis module: Sonnet 4.6 with the prompt in §9 and citation-validation guardrail.
- CLI: `herald ask "<question>"` — prints answer + sources + LOC page image URLs.
- Eval harness: runs all 10 validation questions, prints answers + source lists for hand-grading. Cheap, fast, repeatable.

**Out of scope in Phase 1:** any React/Next code, any auth, any payment, any vision-API call, any UI.

### Phase 2 — Web UI

- Next.js + React app, split-screen layout: chat on the left, image viewer (OpenSeadragon or similar IIIF viewer) on the right.
- Citation click → loads the page image and pans to the chunk's approximate region. (Approximate is fine in Phase 2; precise region highlighting is a Phase 3 nice-to-have that needs JP2 word-level coordinates we don't have from raw OCR.)
- Anonymous read-only access; auth is still Phase 3.

### Phase 3 — Credits, auth, vision OCR

- Supabase Auth (email link / OAuth) wired to the existing `users` table.
- **Row Level Security policies** for `users`, `credit_ledger`, and `reocr_jobs`. Tables were created in Phase 1 *without* RLS (intentionally — `service_role` bypasses RLS and there is no public frontend yet). Policies must be defined and enabled **before the `anon` key is exposed to the browser**. At minimum: a user may read/update their own `users` row; a user may read their own `credit_ledger` rows but never insert (writes go through the credit-spend stored procedure under `service_role`); a user may read their own `reocr_jobs` rows and `pages.reocr_status` is publicly readable.
- Stripe Checkout for credit packs.
- Vision-OCR worker (Claude Sonnet 4.6 vision is the leading candidate; final pick is a Phase-3-day-1 bake-off vs. GPT-4o-vision-class alternatives on a 50-page sample).
- "Clean this page" button in the UI; transactional credit deduction; async result delivery; staleness badges on old citations.
- Optional: word-level coordinate output from vision call to enable precise on-image highlighting.

---

## 14. Risks & open questions

- **LOC OCR quality variance across papers.** The Tribune was a major paper with good source scans; the Albany Evening Journal may have noisier OCR. Phase 1 ingest must not crash on near-empty OCR; chunks below a minimum-content threshold get dropped, not embedded. Acceptable retrieval coverage is an empirical question we won't fully answer until eval.
- **Editorial slant comparison is a *feature*, not just a risk.** Tribune (Whig-reform, Greeley) vs. Evening Journal (Whig-establishment, Weed) gives us a built-in "how do these papers differ on the same story?" demo. But both papers are *Whig* — we don't have a Democratic perspective. If reviewers want a third dataset, the Albany Argus (`sn83030908`, Democratic) is the natural add. Out of scope for now; noted.
- **Page-image URL stability.** LOC URLs are stable in practice but not guaranteed forever; we store the canonical LCCN/date/sequence triple so we can rebuild URLs even if their pattern changes.
- **HNSW recall at 150k+ chunks.** HNSW defaults are usually fine, but if recall@10 is bad on validation questions we may need to tune `m`/`ef_construction`/`ef_search`. Worth measuring early.
- **Reranker free tier.** Voyage's first 200M tokens are free per account; we'll cross that boundary if usage grows. Cost model assumes the free tier holds through demo.
- **Synthesis-model drift.** If we change synthesizer models we should re-run the validation set and diff the answers. Build the eval harness with this in mind.
- **Concurrent re-OCR contention** (Phase 3). Partial-unique index handles "two users at once." Worker-death recovery (janitor cron) needs to be written carefully — running jobs older than N minutes should be reset, but we must not double-spend a user's credit.
- **What counts as a "citation hit" during eval?** We haven't agreed a rubric. Proposal: a citation is "good" if the chunk's text contains the claim or paraphrases it on the page; we hand-grade the first eval pass, then crystallize a rubric, then build an automated grader (LLM-as-judge) for regression.
- **Tone calibration.** The system prompt in §9 is a first draft. Realistic dial-in happens after we read 20 actual responses.
- **Article-boundary detection is deferred, not impossible.** Once a meaningful share of pages have been Cleaned in Phase 3, a downstream pass could rebuild article structure from cleaned text. Out of scope for now.
- **Demo data exclusivity.** "Optional attribution but no exclusive rights" is a product position, not legal text. We need a one-paragraph plain-English TOS for credit-purchasers before Phase 3 ships.
- **Multi-paper schema validated on day one.** With two papers in Phase 1 (instead of one), we exercise the `papers` table, `paper_id` filtering, and cross-paper synthesis from the start. Lower risk of multi-paper bugs appearing later when we add a third corpus.
- **RLS is deliberately absent in Phase 1.** All Phase 1 tables were created without Row Level Security. This is safe today because (a) all writes go through `service_role`, which bypasses RLS, and (b) there is no browser-facing client — only the CLI. **Before the Phase 2/3 web frontend uses the `anon` key, RLS policies must be written and enabled** for `users`, `credit_ledger`, and `reocr_jobs` (see Phase 3 scope above). Shipping a public anon key against unprotected tables would leak credit balances and other users' job history.

---

## 15. Verification (how we'll know Phase 1 works)

1. `herald ingest --lccn sn83030213 …` and `herald ingest --lccn sn83030911 …` both complete end-to-end with no manual intervention; row counts in `papers` (2 rows) / `issues` / `pages` / `chunks` match LOC's totals within ±0.5%.
2. The Supabase project has all 7 tables, all indexes (HNSW + GIN + btree), and `EXPLAIN ANALYZE` on a sample query shows index use.
3. `herald ask "<q>"` returns answer + citations + image URLs for each of the 10 validation questions in §12. Citations are paper-tagged.
4. A human grader (us) marks each answer "passes / partial / fails" against the page images. **All of #1–#8 and #10 must be ≥ "partial"; #9 must be "passes" (a clean refusal).**
5. Citation validator catches a *deliberately injected* hallucinated marker (`[999]`) in a test run.
6. Per-query cost stays under $0.10 in practice (target: $0.06).
7. A cross-paper validation question (e.g. #2 or #7) returns citations from *both* papers, not just one.

When all seven are green, Phase 1 is done and we open the Phase 2 plan PR.

---

## Appendix A — Decisions to call out for review

| Topic | Spec said | Plan recommends | Why |
|---|---|---|---|
| **Corpus** | **New York Herald 1840–1845** | **New-York Daily Tribune + Albany Evening Journal, 1842–1846** (user-approved switch) | Targeted searches showed thin Anti-Rent coverage in the Herald (sensationalist NYC penny press, not state-political). Tribune (Greeley) and Evening Journal (Weed) both engaged the Anti-Rent story directly. Two papers also stress-tests the multi-paper schema. |
| Embedding model | `voyage-3` | `voyage-3.5` | Same price, strictly better retrieval (Voyage's published numbers). Drop-in. |
| Synthesis model | "Claude or GPT, decide at build time" | `Sonnet 4.6` for Phase 1 | Best $/quality for grounded summarization at this corpus size. Revisit before Phase 2. |
| Vision-OCR model | "Claude or GPT, decide at build time" | Phase-3 bake-off; default working assumption Claude Sonnet 4.6 vision | Vision pricing puts a page transcription at ~$0.05 — exactly the "5¢ Cleanup" price. Final pick deferred to Phase 3 day 1. |
| Reranker | Unspecified | Voyage `rerank-2.5` | 32K context, instruction-following, 200M free tokens covers Phase 1 eval. |
| Chunking | 400 / 50 suggested | 400 / 50 confirmed | In the modern RAG sweet spot. |
| Vector index | Unspecified | HNSW | Supabase default; better recall than IVFFlat at our scale; partial-index on `is_current` keeps post-Cleanup hygiene clean. |
| Hybrid merge | Unspecified | RRF k=60 | Supabase docs' default; well-validated starting point. |

---

## Appendix B — PR plan

This plan ships as `docs/PLAN.md` on branch `claude/plan-newspaper-research-37pX7`. The previous version of this plan (NY Herald corpus) was merged in PR #1; this iteration swaps the corpus to Tribune + Albany Evening Journal per verification findings and user approval, and is the only change in this PR. User reviews on phone, requests changes, we iterate. After approval and merge, a separate PR begins Phase 1 implementation.
