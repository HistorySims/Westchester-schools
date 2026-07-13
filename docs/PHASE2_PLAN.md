# Herald Phase 2 — Web UI Plan

> **STATUS (June 2026):** Phase 2 is shipped. The split-screen UI, citation badges, conversation history, response modes, and date/paper filters all work. A few sections of this plan diverged from what was built — most notably the **page viewer**: LOC's Cloudflare protection means we can't render images inline, so the viewer now shows a clean "Open on Library of Congress" card instead of attempting inline display. See `docs/STATUS.md` for the current product description.
>
> **Prerequisite:** Phase 1 is complete. All 20 PRs merged; 110 tests pass; the CLI (`herald ask`) produces well-cited answers for all 10 validation questions.

---

## 1. What Phase 2 delivers

A public-facing web app where a user types a question about the 1842–1846 New York newspaper corpus and gets a synthesized, cited answer alongside the original page images. The core interaction:

1. User types a question in the left pane.
2. The app retrieves, reranks, and synthesizes (same pipeline as the CLI).
3. The answer appears with inline `[N]` citation markers rendered as clickable links.
4. Clicking a citation loads the corresponding LOC newspaper page in a deep-zoom viewer on the right, scrolled/panned to approximate position.

No auth, no payments, no re-OCR — those are Phase 3.

---

## 2. Decisions (user-confirmed)

| Decision | Choice | Rationale |
|---|---|---|
| **Repo layout** | Monorepo, `web/` subdirectory | One review surface; shared CI; no cross-repo coordination |
| **Backend** | Next.js API routes calling Supabase + Voyage + Anthropic directly | Single deploy unit; fast iteration; retrieval/synth logic ported to TypeScript |
| **Image viewer** | OpenSeadragon with LOC's IIIF tile source | Deep-zoom, pan, smooth UX; no image hosting on our side; LOC serves IIIF tiles natively |
| **Auth** | Anonymous read-only, no auth | Per PLAN §13; rate-limit by IP; auth deferred to Phase 3 |

---

## 3. Architecture

```
Browser (Next.js App Router)
  ├── Left pane: chat interface
  │     └── POST /api/ask  ─────────────────────────────────────┐
  │                                                              │
  │                                                              ▼
  │                                              ┌───────────────────────────────┐
  │                                              │   /api/ask  (Route Handler)   │
  │                                              │                               │
  │                                              │ 1. Embed query (Voyage 3.5)   │
  │                                              │ 2. Hybrid search (Supabase)   │
  │                                              │    ├─ HNSW top-50             │
  │                                              │    ├─ FTS top-50              │
  │                                              │    └─ RRF merge (k=60)        │
  │                                              │ 3. Rerank (Voyage rerank-2.5) │
  │                                              │ 4. Optional MMR diversity     │
  │                                              │ 5. Synthesize (Claude 4.6)    │
  │                                              │ 6. Validate citations         │
  │                                              └──────────┬────────────────────┘
  │                                                         │
  │                                              JSON: { text, citations[] }
  │                                                         │
  ├── Right pane: OpenSeadragon viewer  ◄───────────────────┘
  │     └── citation click → load IIIF tile source for page
  │
  └── Shareable URL: /q/<slug>  (stretch goal)
```

### Key constraint

The retrieval + synthesis pipeline is **ported to TypeScript**, not called via a Python subprocess. This means:

- Supabase queries (HNSW + FTS + RRF) are issued via `@supabase/supabase-js` and raw SQL RPCs.
- Voyage embed + rerank calls go through `fetch` against Voyage's REST API.
- Anthropic synthesis uses `@anthropic-ai/sdk`.
- The Python CLI remains the canonical reference implementation; the TypeScript port must produce equivalent results on all 10 validation questions.

---

## 4. Repo layout

```
herald/
├── docs/
│   ├── PLAN.md              # Phase 1 plan (existing)
│   └── PHASE2_PLAN.md       # this file
├── src/herald/              # Phase 1 Python package (untouched)
├── tests/                   # Phase 1 Python tests (untouched)
├── web/                     # NEW — Phase 2 Next.js app
│   ├── package.json
│   ├── tsconfig.json
│   ├── next.config.ts
│   ├── tailwind.config.ts
│   ├── .env.local.example   # SUPABASE_URL, SUPABASE_SERVICE_KEY,
│   │                        # VOYAGE_API_KEY, ANTHROPIC_API_KEY
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx          # split-screen shell
│   │   │   ├── api/
│   │   │   │   └── ask/
│   │   │   │       └── route.ts  # retrieval + synthesis endpoint
│   │   │   └── q/
│   │   │       └── [slug]/
│   │   │           └── page.tsx  # shareable answer page (stretch)
│   │   ├── components/
│   │   │   ├── ChatPane.tsx
│   │   │   ├── MessageBubble.tsx
│   │   │   ├── CitationLink.tsx
│   │   │   ├── ViewerPane.tsx
│   │   │   └── PageViewer.tsx    # OpenSeadragon wrapper
│   │   ├── lib/
│   │   │   ├── supabase.ts       # server-side Supabase client
│   │   │   ├── voyage.ts         # embed + rerank
│   │   │   ├── retrieval.ts      # hybrid search, RRF, MMR
│   │   │   ├── synth.ts          # Claude synthesis + citation validation
│   │   │   └── types.ts          # shared types
│   │   └── hooks/
│   │       └── useViewer.ts      # OpenSeadragon lifecycle
│   └── public/
│       └── openseadragon/        # OSD images (nav icons)
├── pyproject.toml           # existing
└── ...
```

---

## 5. API design

### `POST /api/ask`

**Request:**

```json
{
  "question": "How do the papers report the killing of Sheriff Steele?",
  "paper_lccn": null,
  "date_from": null,
  "date_to": null
}
```

All filter fields are optional. `paper_lccn` restricts to a single paper; dates narrow the window.

**Response (streamed via Server-Sent Events):**

The answer streams token-by-token so the user sees text appearing. Citations are resolved after the full response completes.

```
event: token
data: {"text": "The "}

event: token
data: {"text": "Tribune "}

...

event: done
data: {
  "text": "The Tribune reports that Sheriff Osman Steele was killed...[1]...[2]",
  "citations": [
    {
      "index": 1,
      "chunk_id": "a3f...",
      "paper_title": "New-York Daily Tribune",
      "paper_lccn": "sn83030213",
      "date_issued": "1845-08-09",
      "page_sequence": 2,
      "edition": 1,
      "image_url": "https://chroniclingamerica.loc.gov/lccn/sn83030213/1845-08-09/ed-1/seq-2.jpg",
      "resource_url": "https://www.loc.gov/resource/sn83030213/1845-08-09/ed-1/seq-2",
      "iiif_base": "https://tile.loc.gov/image-services/iiif/service:ndnp:dlc:batch_dlc_...:sn83030213:print:1845080901:0002",
      "snippet": "...the sheriff was shot dead..."
    },
    ...
  ],
  "refused": false,
  "input_tokens": 12400,
  "output_tokens": 680
}
```

**IIIF tile source resolution:** LOC newspaper pages are available through the IIIF Image API. The `iiif_base` URL is derived from the page's resource URL by querying the LOC resource endpoint's `image_url` field at build time. If the IIIF base cannot be resolved, we fall back to the static JPEG URL in a plain `<img>` tag (no deep-zoom, but still functional).

**Rate limiting:** 10 requests per minute per IP, enforced via an in-memory sliding window in the route handler. Returns 429 with a `Retry-After` header when exceeded.

---

## 6. Retrieval pipeline (TypeScript port)

The TypeScript retrieval must reproduce the Python pipeline's behavior:

### 6.1 Hybrid search via Supabase RPCs

Two Postgres functions, created as a Phase 2 migration:

```sql
-- Semantic search: HNSW cosine similarity
create or replace function match_chunks_semantic(
  query_embedding vector(1024),
  match_count int default 50,
  filter_paper_lccn text default null,
  filter_date_from date default null,
  filter_date_to date default null
) returns table (
  chunk_id uuid,
  content text,
  page_id uuid,
  paper_lccn text,
  paper_title text,
  date_issued date,
  edition int,
  page_sequence int,
  image_url text,
  resource_url text,
  similarity float
) ...

-- Full-text search: GIN tsvector
create or replace function match_chunks_fts(
  query text,
  match_count int default 50,
  filter_paper_lccn text default null,
  filter_date_from date default null,
  filter_date_to date default null
) returns table (
  chunk_id uuid,
  content text,
  page_id uuid,
  paper_lccn text,
  paper_title text,
  date_issued date,
  edition int,
  page_sequence int,
  image_url text,
  resource_url text,
  rank float
) ...
```

These join `chunks → pages → issues → papers` and apply the optional filters, keeping the heavy lifting in Postgres where the indexes live.

### 6.2 RRF merge

Same formula as Phase 1: `score = Σ 1/(k + rank_i)` with `k = 60`.

### 6.3 Rerank

Top ~80 RRF results → Voyage `rerank-2.5` → top 20.

### 6.4 MMR diversity

Same λ=0.5 conditional logic as Phase 1. Applied for breadth queries; skipped for narrow factual queries.

### 6.5 Parity validation

Before Phase 2 ships, run all 10 validation questions through both the Python CLI and the TypeScript API. The citation sets must overlap ≥ 80% and the synthesis answers must be graded equivalently.

---

## 7. Synthesis (TypeScript port)

Uses `@anthropic-ai/sdk` with streaming enabled. The system prompt, user-turn template, and citation-validation logic are identical to `src/herald/synth.py`, with these adjustments:

- **Streaming:** Uses `messages.stream()` to yield tokens to the SSE response.
- **`max_tokens`:** 2500 (matching the cleanup commit's bump).
- **Citation validation:** Runs after the stream completes. On hallucinated markers, retries once (non-streamed) with the stronger reminder, same as the Python version. The retry response is sent as a single `done` event.
- **Refusal heuristic:** Same tightened logic — canonical phrase AND zero `[N]` markers.

---

## 8. UI components

### 8.1 Split-screen layout (`page.tsx`)

A full-viewport flex container:
- **Left pane (50% default, resizable):** Chat interface with question input at bottom, messages scrolling upward.
- **Right pane (50% default):** OpenSeadragon viewer, initially showing a welcome/placeholder state. A drag handle between panes allows resizing.
- **Mobile (<768px):** Stack vertically; viewer slides up as a drawer on citation click.

### 8.2 ChatPane

- Text input with submit button (Enter to send, Shift+Enter for newline).
- Optional filter controls (collapsed by default): paper selector dropdown, date range picker.
- Message history: user messages right-aligned, assistant messages left-aligned.
- Streaming text renders incrementally via the SSE connection.

### 8.3 MessageBubble + CitationLink

- Citation markers `[N]` in the assistant's text are parsed and rendered as `<CitationLink>` components: styled as superscript numbered badges.
- Hovering a citation shows a tooltip with paper name, date, page.
- Clicking a citation:
  1. Highlights the citation badge.
  2. Sends the citation's IIIF tile source URL to the viewer pane.
  3. The viewer loads (or navigates to) that page.

### 8.4 ViewerPane + PageViewer (OpenSeadragon)

- `PageViewer` wraps OpenSeadragon in a React component with `useEffect` lifecycle management.
- Tile source: LOC IIIF Image API endpoint for the cited page.
- On citation click: if the viewer is already showing the same page, no-op; otherwise, open the new tile source with a smooth transition.
- Controls: zoom in/out buttons, home (fit-to-page), full-screen toggle.
- Fallback: if IIIF endpoint is unreachable (rare), display the static JPEG in a scrollable `<img>`.

### 8.5 IIIF tile source derivation

LOC exposes IIIF endpoints for Chronicling America pages. The pattern:

```
https://tile.loc.gov/image-services/iiif/service:ndnp:{batch}:{lccn}:print:{date_compressed}:{seq_padded}/info.json
```

The batch identifier varies per paper and isn't predictable from LCCN alone. We resolve it by:

1. At ingest time (Phase 2 migration): for each page, hit the LOC resource endpoint (`resource_url + ?fo=json`) and extract the `image_url` field, which contains the full IIIF-resolvable path. Store this as a new `iiif_info_url` column on the `pages` table.
2. At query time: the citation response includes the pre-resolved `iiif_info_url`, which OpenSeadragon loads directly.

If the migration backfill is too slow for the initial deploy, we can resolve lazily (on first citation click, with a client-side cache).

---

## 9. Styling

- **Framework:** Tailwind CSS.
- **Color palette:** Warm, archival tones — cream/parchment background (`#faf7f0`), dark brown text (`#2c1810`), muted gold accents for citations (`#b8860b`). The viewer pane has a neutral dark background (`#1a1a1a`) to let the page image pop.
- **Typography:** Serif for the assistant's synthesized answers (to echo the newspaper source material). Sans-serif for UI chrome and user input. Monospace for citation metadata.
- **Responsive breakpoints:** Desktop ≥1024px (side-by-side), tablet 768–1023px (side-by-side, narrower chat), mobile <768px (stacked with drawer).

---

## 10. Deployment

### Target: Vercel

- **Why:** Zero-config for Next.js; edge functions for the API route; automatic preview deploys on PRs.
- **Environment variables:** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`. All set in Vercel's project settings, not committed.
- **Region:** `iad1` (US East) — closest to Supabase's free-tier region and LOC's servers.
- **Edge vs. Node runtime:** The `/api/ask` route uses Node.js runtime (not Edge) because `@anthropic-ai/sdk` and the Supabase client work best there. Static pages use Edge.

### Domain

`herald.historysims.com` (or similar) via Vercel custom domain. The LOC page images are served directly from `tile.loc.gov` — no proxy, no CORS issues (LOC serves CORS headers on IIIF endpoints).

---

## 11. Database migration (Phase 2)

A single SQL migration adds:

1. **`pages.iiif_info_url`** column (`text`, nullable) — pre-resolved IIIF `info.json` URL for each page.
2. **`match_chunks_semantic`** RPC function.
3. **`match_chunks_fts`** RPC function.

The IIIF URL backfill runs as a one-time script after the migration, hitting LOC's resource endpoint for each page (throttled, resumable — same pattern as Phase 1 ingest).

---

## 12. Rate limiting & abuse prevention

Without auth, abuse prevention is IP-based:

| Limit | Value | Enforcement |
|---|---|---|
| Requests per minute per IP | 10 | In-memory sliding window in route handler |
| Max question length | 500 characters | Client + server validation |
| Max concurrent requests per IP | 2 | Semaphore in route handler |

The rate limiter resets on deploy (stateless). For Phase 2's anonymous traffic this is sufficient. Persistent rate limiting (Redis / Upstash) is a Phase 3 concern if abuse materializes.

---

## 13. Testing strategy

### Unit tests (Vitest)

- `retrieval.ts`: RRF merge logic, MMR scoring, filter construction — pure functions, no network.
- `synth.ts`: citation extraction, refusal heuristic, user message formatting — mirroring `tests/test_synth.py`.
- `voyage.ts`: request batching, retry logic — mocked HTTP.

### Integration test

- A single end-to-end test that calls `/api/ask` with a known question against the real Supabase database and asserts that the response contains expected citation papers/dates. Runs in CI against the Supabase project (requires env vars).

### Parity test

- A script that runs all 10 validation questions through both `herald ask` (Python CLI) and `POST /api/ask` (TypeScript), then compares citation overlap and answer quality. This is manual/semi-automated — run before each deploy, not in CI.

### Component tests (React Testing Library)

- `CitationLink`: renders badge, fires callback on click.
- `ChatPane`: renders messages, handles submit.
- `PageViewer`: initializes OpenSeadragon with correct tile source.

---

## 14. Build slices

### Slice 1: Scaffolding + API route (no UI)

- `web/` directory: Next.js 15, TypeScript, Tailwind, ESLint, Vitest.
- `lib/supabase.ts`, `lib/voyage.ts`, `lib/retrieval.ts`, `lib/synth.ts` — TypeScript ports of the Phase 1 pipeline.
- `POST /api/ask` route handler — returns JSON (no streaming yet).
- Supabase migration: `match_chunks_semantic` + `match_chunks_fts` RPCs.
- Unit tests for retrieval + synth pure functions.
- Parity check: 10 validation questions, Python vs. TypeScript.

### Slice 2: Split-screen UI + citation click

- `page.tsx` split-screen layout.
- `ChatPane`, `MessageBubble`, `CitationLink` components.
- `ViewerPane` + `PageViewer` with OpenSeadragon.
- Citation click → viewer loads the page's IIIF tile source.
- Tailwind styling (archival palette).

### Slice 3: Streaming + polish

- SSE streaming from `/api/ask`.
- Incremental text rendering in `ChatPane`.
- Rate limiting (IP-based).
- Mobile responsive layout (stacked with drawer).
- Loading states, error handling, empty states.
- Filter controls (paper, date range) — collapsed by default.

### Slice 4: IIIF backfill + deploy

- Database migration: `pages.iiif_info_url` column.
- Backfill script: resolve IIIF URLs from LOC resource endpoints.
- Vercel deployment config.
- Custom domain setup.
- Smoke test on production.

### Stretch: Shareable URLs

- `/q/[slug]` route that persists a question + answer and serves it as a static page.
- Requires a `queries` table (question text, answer text, citation IDs, created_at).
- Social meta tags (og:title, og:description, og:image with a page thumbnail).

---

## 15. Cost model (Phase 2 additions)

Phase 2 adds hosting cost; per-query API cost is unchanged from PLAN.md §11.

| Component | Cost |
|---|---|
| Vercel Pro (if needed for team features) | $20/mo |
| Vercel bandwidth (generous free tier) | $0 initially |
| Supabase (free tier, shared infra) | $0 |
| Per-query API cost | ~$0.06 (unchanged) |
| IIIF backfill (one-time LOC fetches) | $0 (LOC is free) |

At 100 queries/day the API spend is ~$6/day, $180/month. Manageable for a demo; auth + rate limiting in Phase 3 controls this if traffic grows.

---

## 16. Risks & mitigations

- **IIIF endpoint availability.** LOC's tile server is generally reliable but not SLA'd. Mitigation: fallback to static JPEG on IIIF failure; cache `info.json` responses in the browser.
- **TypeScript port parity.** Subtle differences in RRF scoring or MMR could produce different citation sets. Mitigation: the parity test suite; shared test fixtures.
- **Rate limiting without persistence.** The in-memory rate limiter resets on deploy/restart. Mitigation: Vercel's serverless functions have short lifetimes anyway; for Phase 2 traffic levels this is acceptable. Upgrade to Upstash Redis if needed.
- **OpenSeadragon bundle size.** ~300KB gzipped. Mitigation: dynamic import (`next/dynamic`) so it only loads when the viewer pane is active.
- **LOC CORS on IIIF.** LOC serves `Access-Control-Allow-Origin: *` on IIIF endpoints (confirmed empirically). If this changes, we'd need a thin proxy — but LOC has maintained open CORS for years.
- **Supabase service key in API route.** The service key bypasses RLS and must never reach the client. Mitigation: only used in server-side route handlers; Next.js server components never serialize it to the client bundle. Validated by checking `typeof window === 'undefined'` at the Supabase client init.

---

## 17. Acceptance criteria

Phase 2 is done when:

1. A user can visit the web app, type any of the 10 validation questions, and receive a streaming answer with clickable citations.
2. Clicking a citation loads the corresponding page image in the OpenSeadragon viewer with deep-zoom.
3. Citation validation works identically to the CLI — hallucinated markers trigger retry, then error.
4. The app is deployed on Vercel with a custom domain.
5. Rate limiting prevents >10 requests/minute from a single IP.
6. The UI is responsive on desktop (≥1024px) and mobile (<768px).
7. TypeScript parity: all 10 validation questions produce answers graded ≥ "partial" (same bar as Phase 1).
