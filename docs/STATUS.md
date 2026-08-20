# Westchester Schools — Project Status

*Last updated: 2026-08-15*

A semantic-research corpus of Westchester County public-school governance
— board agendas, minutes, policy manuals, student handbooks, teacher
contracts, budgets — built by forking the **Herald** engine (originally
1840s newspapers; see [`HERALD_NEWSPAPER_STATUS.md`](HERALD_NEWSPAPER_STATUS.md)
for the roots). The engine (chunk → embed → cluster → drift → brief →
dossier) transfers almost unchanged; the work so far has been the
**acquisition layer** — getting the documents.

Everything is designed to run from a phone: acquisition is GitHub Actions
`workflow_dispatch` matrix workflows (one job per district = one runner
IP), no local machine required.

For where this is headed — the move to Vercel and the **monthly ingest +
brief** cycle (freeze-and-assign, 6-month/​signal re-cluster, seasonal
year-over-year drift signals) — see [`ROADMAP.md`](ROADMAP.md).

---

## Where we are right now

**Acquisition works end-to-end for all 8 peer districts.** Two independent
source pipelines are built, tested (49 tests green), and verified against
live sites:

1. **BoardDocs** (`go.boarddocs.com`) — agendas, minutes, per-item
   attachments. API fully reverse-engineered and downloading real PDFs.
2. **District websites** — handbooks, contracts, policies, budgets, hosted
   three different ways (native PDF / Google Drive/Docs / Finalsite
   resource-manager). Crawler handles all three.

The **structural chunker** is built and validated on a real Peekskill
agenda. The **ingest pipeline is live** (`herald-ingest`): the first real
run (2026-07-18, crawl-sites run `29528495341`) embedded **11,871 chunks
from 731 documents** across all 8 districts into Supabase — filterable by
district, ordered chronologically, each carrying its section-path. The
downstream engine (cluster → drift → brief → dossier) is inherited and
not yet wired to this corpus.

**First-ingest counts (district-website docs only):** port-chester-rye
3873, ossining 1770, mount-vernon 1494, peekskill 1479, white-plains
1091, tarrytowns 1029, elmsford 962, greenburgh-central 173. Outcome:
731 ingested, 27 skipped (resumed after a mid-run fix), 37 no_text, 1
error.

**Second ingest — BoardDocs agendas/minutes** (2026-07-18, scrape-all run
`29385193938`): 1620 ingested, 41 skipped, 263 no_text, 5 errors, **11,237
more chunks**. Per-district this pass: tarrytowns 6558, ossining 2035,
elmsford 1820, peekskill 421, greenburgh-central 166, white-plains 151,
mount-vernon 54, port-chester-rye 32 — a very different shape than the
site pass (Tarrytown's BoardDocs backs up nearly every consent-agenda
item as its own attachment; Port Chester's BoardDocs meetings, by
contrast, are mostly scanned into `no_text`).

**Corpus totals after both passes: 23,108 chunks, all 8 districts.**
Combined per-district (site + BoardDocs): tarrytowns 7587,
port-chester-rye 3905, ossining 3805, elmsford 2782, peekskill 1900,
mount-vernon 1548, white-plains 1242, greenburgh-central 339.
**Greenburgh remains the outlier by an order of magnitude** — worth
weighting for before any cross-district comparison (e.g. per-district
normalization, not raw counts).

**Query surface — built and working:** **`herald-ask` + the `ask`
workflow** — *panel retrieval* (per-district semantic + FTS + RRF +
rerank, per-document cap, empty districts reported explicitly) feeding
cited Sonnet synthesis. Designed for the corpus's real question shapes
(norm / coverage / outlier — see [`ASK.md`](ASK.md)) rather than global
top-k RAG. Verified live on all three archetypes; each answer prints its
token/cost. Needs `ANTHROPIC_API_KEY` (evidence-only mode works without).

**Visualization surface — built + tuned:** the **topic map** (cluster
scatter) — first of four planned views (see [`VIZ.md`](VIZ.md)).
`herald-cluster` + the `cluster` workflow re-embed content-only, UMAP
to a mid dimensionality (`cluster_dims=10`), HDBSCAN, Haiku labels, and
export compact columnar JSON. A parameter **sweep** (`cluster-sweep`
workflow) settled the knobs: `cluster_dims` barely matters (keep 10);
`min_cluster_size` is a pure granularity dial where fine (15) is both
cleanest (best DBCV) and lowest-noise but yields too many topics for a
flat legend. So the map is now **hierarchical** — cluster fine for
clean leaves, then merge leaf centroids agglomeratively into coarser
tiers (`--tiers 15,60`, guaranteed nesting), each tier Haiku-labelled.
`viz/cluster_map.html` renders it as a **drill-down tree** (theme →
topic → leaf; tapping a branch isolates its points) plus the canvas
scatter (pan/zoom/pinch, district filter, color-by toggle, both themes,
mobile bottom sheet) — verified headless. Delivery: run `cluster`,
share the JSON, it's published as a phone-viewable Artifact. The other
three views (trajectory, district comparison, dossier) build on the
same export.

**Quality gate — built:** **`herald-score` + the `score` workflow** flip
junk chunks to `status='quarantined'` (which both the map and Ask already
filter out): *garbled* text (symbol-soup, judged by the share of tokens
that are real EN/ES words or clean numeric data — so budget tables read as
data and survive) and *procedural boilerplate* (roll calls, motions,
adjournments). Reversible; re-score to move the bar.

**Web app — built (v1):** a Next.js app in `web/` reading Supabase — an
**ask** page (streaming cited answers + global full-text search) and an
**explore** page (the topic map via a self-contained canvas renderer). Server
-side queries make full-text search free (no shipped index). Actions writes,
Vercel reads, Supabase is the seam.

---

## Structured data & analytical queries (the current workstream)

RAG answers *semantic* questions well but **cannot** answer *parametric* ones
over tables — *"which district has the steepest MA+30 salary steps between
years 10 and 20?"* The fix (design in [`STRUCTURED.md`](STRUCTURED.md)) is to
extract the high-value tables into structured rows and answer with a **query**,
not retrieval. Four pieces, all built:

1. **Table-aware chunking** (migration `0002`) — at ingest, whole tables are
   kept intact as `kind='table'` chunks (PyMuPDF `find_tables` → markdown)
   instead of being smeared across prose chunks. Retrieval finds the whole
   grid; extraction gets a clean, header-bearing input.
2. **Corpus backfill** — `herald-ingest tables-db` re-fetches every already
   -ingested document straight from its stored `source_url` (no expired scrape
   artifacts, no content-hash matching) and attaches its tables. Resumable
   (migration `0003`, a `tables_extracted_at` marker). **Result: ~5,576 whole
   -table chunks** across the budget/salary/stipend material that's Google/
   Finalsite-hosted. BoardDocs-hosted docs (~1,729) **fetch-fail** — see below.
3. **Structured extraction** — `herald-extract` (migration `0004`:
   `salary_schedule`, `stipend_schedule`) feeds each schedule-like table chunk
   to Claude for JSON rows, normalizes lanes/positions **deterministically** in
   `taxonomy.py` (canonical lanes + a hand-maintained `data/lane_crosswalk.csv`),
   and runs audit invariants (monotonic salary up a lane, higher lane ≥ lower at
   the same step, year-over-year non-decreasing, $30k–$250k sanity) in
   `--dry-run` before any write. Idempotent; each chunk stamped `extracted_at`.
   First real run: **885 stipend rows across 50 tables** (coach/extra-duty pay —
   the "who pays coaches unusually much" question is answerable now). Salary was
   initially **zero** — a bug (salary rows require a `school_year`, but CBA grids
   don't repeat it and contracts have no meeting date, so every row was dropped);
   fixed by pulling the year from the document title + streaming the call.
   **Re-running now to populate `salary_schedule`.**
4. **Analytical query path** (`analytical.py`) — `herald-ask --mode auto`
   routes each question with a cheap Haiku call: *semantic* → today's panel RAG;
   *analytical* → one of four **hand-written templated queries** (`step_slope`,
   `max_at_step`, `stipend_compare`, `delta_over_years`) over the structured
   tables. The ranking is computed in **SQL — numbers never pass through the
   model** — and rendered as a cited answer, with districts lacking an extracted
   grid listed as "not available" and percent-of-base stipends reported
   separately. Failure mode is "unsupported question," never wrong SQL.

### Contracts live outside board docs — and are often scanned (the key pivot)

A reframe (user, 2026-08-15) redirected the whole salary effort: **teacher
salary schedules mostly are not in board-docs at all.** The first extract
runs confirmed it — only spurious rows came out, because the board-docs
corpus barely contains a real teacher grid. CBAs live on **union sites and
district HR pages** (open-gov / "contracts" pages), not in meeting packets.
So we built a **contract-acquisition crawler** (`herald-scrape contracts` +
`crawl-contracts.yml`, matrix ×8, seeds in `data/targets/cba_sources.json`)
that walks those sources. It reached contract-type PDFs for **6/8 districts**
on the first pass (2 dead seeds since fixed); the download run pulled real
contracts for **7/8**.

Two more corrections shaped the schema and the priorities:

- **"A custodian contract isn't a throwaway."** A district runs on more than
  its teachers. The salary layer is no longer teacher-only: migration `0005`
  adds a **`bargaining_unit`** dimension (teacher/administrator/custodial/
  aide/clerical/nurse/…) to `salary_schedule` and `stipend_schedule` and to
  their unique keys, so a custodial grid and a teacher grid for the same
  year coexist instead of colliding; the extractor prompt classifies any
  staff schedule (not just teacher) and tags the unit; audits run per unit.
- **"We're not just talking salary."** A CBA's primary value is its **full
  prose** — "which districts offer paternity leave," grievance, class-size
  caps — answered by semantic search once the contract is *ingested*,
  independent of whether any salary row is ever extracted. Structured tables
  are the secondary layer.

**But the flagship CBAs are scanned images.** Ingesting the crawl showed the
teacher contracts (`Tarrytown-TAT-2022-2025`, `WPTA MOA`, `MVAG MOA`) come
back `no_text` — no text layer, so neither prose nor tables reach the
corpus. Tesseract recovers prose but flattens salary grids into unusable
number-jumble. So the OCR path gained a **Claude-vision engine**
(`ocr_pdf_vision` + `herald-ingest ocr --engine vision`): it transcribes
each scanned page to Markdown **keeping salary grids as tables**, which are
carried as `kind='table'` chunks — one vision pass feeds both the semantic
layer (prose) and the numeric layer (`herald-extract` reads the tables into
`salary_schedule`). Tesseract stays as the cheap prose-only engine.

**Current milestone:** vision-OCR the scanned CBAs (proving on tarrytowns'
TAT first — dry run confirmed **14 no_text candidates**, of which 3 are real
CBAs, 3 junk HTML-as-PDF, 8 scanned budgets), then `herald-extract` to land
real per-unit `salary_schedule` rows, making the flagship *"steepest MA+30
step 10→20"* question answerable end-to-end. The stipend layer already works
(885 rows). The inherited drift/brief engine remains unwired; budgets
(phase 2b) are deferred.

**Keeping it current:** the monthly, update-only crawl→ingest→OCR→extract
refresh is designed in [`REFRESH.md`](REFRESH.md) — one scheduled workflow,
change-detected by extracted-text hash (drift-proof), that also retires the
run-id hand-carrying between stages.

### 📌 Pinned: where the salary thread stopped (2026-08-20)

Paused mid-flight to fix the policy gap below. To resume, this is the state:

- **Vision OCR works.** 13 scanned docs recovered, 776 chunks (elmsford 431,
  greenburgh 178, tarrytowns 104, mount-vernon 49, white-plains 14). The
  scanned CBAs are now searchable **prose**, so contract *policy* questions
  (leave, grievance, class size) already work against them.
- **Salary grids are still not extracted.** Four causes found and fixed in
  sequence — a rotated-page renderer, a Postgres-invalid `\b` in the
  candidate regex, a trailing space in a workflow input, and (the real one)
  a **silent `max_tokens` truncation**: vision runs adaptive thinking by
  default and the budget covered thinking + output together, so the one dense
  rotated grid page burned it on thinking and emitted nothing, unflagged.
  Fixed: 32k budget, streaming, loud `stop_reason` warning.
- **Not yet verified.** The re-OCR of tarrytowns with those fixes has *not*
  been run. Next step on resume: `ocr` (vision, reocr, tarrytowns) →
  `extract --reextract`, and check the grid's known values survive
  (Appendix A step 1 BA = 63,541 … step 17 DR = 146,177).
- **Known-good side effect:** the candidate-filter fix took tarrytowns from
  0 → 28 candidate tables and yielded **12 stipend rows** from a personnel
  agenda, clean audit. Board-docs tables were invisible before it.
- **Still open:** White Plains' CBA is born-digital with *image-only* pages
  (49, 58–66) holding its grids; document-level `no_text` never flags it, so
  **page-level OCR** remains unbuilt. Port Chester's CBA is fully scanned
  (40 pages, zero text). `tarrytownlearningcenter.org` turns out to host
  contracts for *multiple* districts — a better acquisition target than
  eight separate union sites.

---

## Board policies — the corpus's biggest hole (current priority)

A live question exposed it: *"which districts automatically deny course
credit at N unexcused absences?"* returned "only Tarrytowns" with an honest
hedge — while Port Chester's Policy 5100 says exactly that (12 absences →
meeting; 18 → written notice of credit risk; 25 → credit loss). The answer
layer behaved correctly; the corpus simply didn't contain the policy.

**What we actually hold is a thin, accidental slice.** Policies reach the
corpus two ways, neither of them the manual: whatever loose PDFs a district
website happens to link (Port Chester: 18 discovered, **13 ingested**, none
of them attendance), and whatever draft rides in as a board-meeting
attachment while being amended (which is why Tarrytown's 5100 is present —
and why its text still carries NYSSBA template brackets and "this is
illustrative" boilerplate). A NY district manual runs to several hundred
policies, so we hold ~4% of one district's. **Every policy question can
return a confident false negative.**

**Where the manuals actually live** (surveyed 2026-08-20 from each district's
BoardDocs `/Public` config): all eight report
`bd.policy_connected="NyssbaManagementConsole"` — the manual is **not** in
BoardDocs. BoardDocs *does* expose a policy console
(`BD-GetPolicyBooks`/`BD-GetPolicies`/`BD-GetPolicyItem`, recovered from
`policies.js`) but returns **`No Access`** to anonymous callers. The manuals
are on two third-party portals:

| District | Portal |
|---|---|
| port-chester-rye | `boardpolicyonline.com/?b=port_chester_rye` (+ 7 section deep links) |
| peekskill | `boardpolicyonline.com/?b=peekskill` |
| ossining | `policy.microscribepub.com` (infobase `ossining.nfo`) |
| elmsford | `policy.microscribepub.com` (infobase `elmsford.nfo`) |
| tarrytowns, mount-vernon, greenburgh-central, white-plains | not yet pinned — discover from district site |

Port Chester's own `/board/policies` page links the manual *and* the loose
PDFs; the site crawler took the PDFs and walked past the manual, because it
follows same-host pages and cross-domain **PDFs** — and a portal is a
cross-domain **HTML app**.

**Built so far:** `herald-scrape policy-probe` + `policy-probe.yml` —
reconnaissance only (find each district's portal, capture what it returns:
redirects, framesets, scripts, forms), because both vendors sit outside this
project's egress allowlist and can only be reached from a networked runner.
Targets in `data/targets/policy_portals.json`. The parser gets written from
the captured bodies, not guessed — the same path the BoardDocs API took.

**Test suite: 240 green.**

---

## The peer set

Eight districts chosen as demographic/socioeconomic peers of Port Chester
(the anchor). Verified BoardDocs slugs + district domains in
`data/targets/port_chester_peers.json` and [`DATA_SOURCES.md`](DATA_SOURCES.md):

| District | BoardDocs slug | Website |
|---|---|---|
| Port Chester-Rye | pcru | portchesterschools.org |
| Ossining | oufsd | ossiningufsd.org |
| Peekskill | pcsd | peekskillcsd.org |
| Tarrytowns | tufsd | tufsd.org |
| Elmsford | elmsford | eufsd.org |
| Mount Vernon | mvcsd | mtvernoncsd.org |
| Greenburgh Central | greenb | greenburghcsd.org |
| White Plains | wpcsd | whiteplainspublicschools.org |

Yonkers is tracked under `non_boarddocs` (different platform, not yet
adapted).

---

## Successes

- **BoardDocs API fully reverse-engineered.** Committee id lives in the
  `/Public` HTML (`committee-trigger` / `<select name="committeeid">`);
  `BD-GetMeetingsList?open` lists meetings; `PRINT-AgendaDetailed?open`
  returns the agenda HTML with `/$file/` attachment links. Browser-mode
  headers + priming a `/Public` load defeats the 403. One matrix job per
  district gives each its own IP, sidestepping BoardDocs' per-IP
  rate-limiting.
- **Politeness is real, not cosmetic.** `Fetcher` enforces a minimum
  request interval (default 3s) + jitter, bounded retries that honor
  `Retry-After`, and a robots policy. Public records + `--ignore-robots`
  only where justified.
- **All three district-site hosting patterns solved live**, each found by
  a district returning 0 docs and then fixing the real cause:
  - Ossining → docs on **Google Drive/Docs** → `gdrive_download_url`
    (0 → 122 docs live).
  - Port Chester → docs on **Finalsite resource-manager** →
    `_FINALSITE_DOC` detection (0 → 20 live).
  - JS-rendered nav hiding links → **sitemap.xml seeding**
    (`sitemap_urls`) so we don't depend on crawlable `<a>` tags.
- **Structural chunker validated on real documents.** Chunks on the
  agenda's own numbered outline (`P13.D` addressing), captures the
  hierarchical section path as chunk metadata, adaptive granularity
  (narrative whole / consent-agenda merged / oversize window-split). This
  is the "topic clustering that already exists in the documents" the
  project wanted. See [`CHUNKING.md`](CHUNKING.md).
- **Deep dry pass confirmed coverage across all 8 districts** (~800 docs
  discovered): handbooks, contracts, minutes, and policies now appear
  everywhere — not just budgets, which was the shallow-crawl failure mode.

### Deep-dry coverage snapshot (2026-07-16, pre-download)

| District | Total | handbook | contract | minutes | agenda | policy | budget |
|---|---|---|---|---|---|---|---|
| port-chester-rye | 250 | 1 | – | 152 | 43 | 18 | 36 |
| white-plains | 116 | 4 | 1 | 29 | 20 | 16 | 46 |
| ossining | 127 | 2 | – | 91 | 2 | 4 | 28 |
| peekskill | 96 | 1 | 4 | – | – | 6 | 85 |
| mount-vernon | 70 | 1 | 26 | – | – | 6 | 37 |
| elmsford | 64 | 3 | 1 | – | – | 9 | 51 |
| tarrytowns | 58 | 3 | 7 | – | – | 3 | 43 |
| greenburgh-central | 22 | 1 | – | – | – | – | 21 |

---

## Failures, weak spots & known issues

- **BoardDocs now IP-blocks datacenter fetches.** The original July scrape
  downloaded BoardDocs files fine, but re-fetching them for the table backfill
  now 403s — even through headless Chromium (Playwright), which rules out
  headers/fingerprint/session and points to **IP-reputation blocking of GitHub's
  runner ranges** (BoardDocs tightened since July). So the `tables-db` backfill
  loses ~1,729 BoardDocs-hosted docs — mostly minutes/policies/CSE rosters (low
  table value), but also some standalone stipend sheets. The docs are still in
  the corpus as **prose** from July; we just can't re-pull them for tables. The
  block is on datacenter IPs, not browsers — so the realistic fix for any
  specific high-value doc (a district's CBA) is a **manual download from a phone/
  home network** into a small manual-ingest path, not proxy infrastructure.
- **~300 scanned PDFs have no text layer (`no_text`) → OCR now has a
  vision engine; first real pass running (tarrytowns CBA).** The scanned
  set includes the highest-value docs — the teacher CBAs — so the OCR path
  gained a Claude-**vision** engine that keeps salary grids as tables (see
  the contracts pivot above); Tesseract remains the cheap prose-only engine.
  These aren't random: older Port Chester agendas
  (2019–2021, ~20 docs — over half the *site*-crawl no_text), plus in the
  BoardDocs pass a large tail of small scanned consent-agenda backups
  (fixed-asset disposal forms, bid awards, club charters, individual
  MOAs) — and, most important, **teacher contracts** (Peekskill `PAA CBA
  2025-2028`, Mount Vernon `MVAG MOA 2022`). Some of the highest-value
  documents are scanned images. **`herald-ingest ocr` + the `ocr`
  workflow** handle this: Tesseract via PyMuPDF rasterization (CPU-only,
  free, no new key/allowlist), reprocessing only `no_text` docs → chunk →
  embed → update-in-place. A fast dry run counts candidates per district
  (no OCR/keys) before spending. **Must run before ~July 29–30** (needs
  the scrape artifacts, 14-day retention). The pipeline records these as
  `documents.ingest_status='no_text'` so they stay queryable.
- **One `.bin` download can't be parsed** (Greenburgh "Budget WorkShop #4")
  — the server didn't declare a content type, so it saved as `.bin` and
  PyMuPDF refused it. 1 document; recorded as `ingest_status='error'`.
- **Legacy Office files (`.doc`, `.ppt`) aren't extractable.** Found in the
  BoardDocs pass: Tarrytown personnel agendas saved as `.doc`, a board
  summary as `.ppt`. PyMuPDF only reads PDF (and a few image formats), so
  these 5 fail with `ingest_status='error'`. Fix is a separate extractor
  (`python-docx`/`python-pptx`) or a LibreOffice-headless PDF conversion
  step before ingest — not urgent (5 documents so far) but will recur as
  more BoardDocs districts are ingested, since older attachments are
  often plain Office files rather than PDFs.
- **The no_text backlog grew a lot with BoardDocs** (263 in the second
  pass alone, vs. 37 from the site crawl) — mostly small consent-agenda
  backup attachments (fixed-asset disposal forms, bid awards, club
  charters, individual MOAs) that districts scan as images rather than
  export as text PDFs. Tarrytown's BoardDocs practice — one attachment
  per agenda line item — means it has by far the most of these. Same
  fix as before (targeted OCR), just a bigger list now; not blocking
  since the corpus is usable without them.
- **Greenburgh Central is thin** — 22 docs, almost all budget, no
  minutes/agenda/contract discovered. Either a sparser site or a nav/
  sitemap pattern the crawler isn't reaching. **Deliberately deferred as a
  known follow-up** (user decision, 2026-07-16); the other seven are
  strong enough to proceed. First thing to check: whether its sitemap
  lives at a non-standard path or the site is fully JS with a `/documents`
  or `/departments` index we're not seeding.
- **`budget` is over-represented.** Districts genuinely post many budget
  PDFs (multi-year adopted/proposed/presentation decks), so this is
  over-*collection*, not misclassification. Left as-is deliberately —
  better to over-collect budgets than to miss a handbook. Revisit only if
  it crowds out the corpus.
- **BFS coverage skew in shallow crawls.** With a low `max_pages` the
  crawler exhausts its budget inside the finance section before reaching
  handbooks. Mitigated by deeper `max_pages` (120) + sitemap seeding;
  worth remembering if a new district comes back budget-only.
- **Meeting date + doc_type are best-effort at scrape time.** They're
  inferred from URL/anchor text now; the authoritative pass happens at
  **ingest** (from document content). Don't trust scrape-time `date`/
  `doc_type` as final.
- **Yonkers not yet adapted** — different platform, no adapter written.
- **Engine half is untouched for this corpus.** Embedding, clustering,
  drift, Brief, and Dossier are inherited from the newspaper repo and not
  yet pointed at school documents. No database has been populated.

---

## Architecture at a glance

```
BoardDocs adapter ─┐
                   ├─► ScrapedDoc ─► Fetcher ─► RawStore (content-hashed files)
District-site ─────┘                       └─► Manifest (append-only jsonl, dedupe)
crawler                                             │
                                    ingest adapter ◄┘   (NEXT milestone — not built)
                                          │
                        chunk ─► quality filter ─► embed (Voyage) ─► pgvector DB
                                          │
                              cluster ─► drift ─► Brief ─► Dossier   (inherited engine)
```

- `src/herald/scrape/` — acquisition. `core.py` (Fetcher/RawStore/Manifest),
  `boarddocs.py`, `site.py`, `runner.py`, `models.py`, `__main__.py`
  (Typer CLI `herald-scrape`). See [`SCRAPING.md`](SCRAPING.md).
- `src/herald/chunking.py` — structural agenda chunker. See
  [`CHUNKING.md`](CHUNKING.md).
- `.github/workflows/` — `scrape.yml` (single), `scrape-all.yml`
  (BoardDocs matrix ×8), `crawl-sites.yml` (district-site matrix ×8),
  `probe.yml`.
- Managed by `uv` (`uv sync --frozen` in CI). PDF text via **PyMuPDF**
  (`fitz`) — pypdf crashed on `_cffi_backend`/cryptography in this env.

---

## Design decisions worth remembering

- **Keep the package named `herald`** to remember the project's roots
  (deliberate revert of a mechanical `herald`→`schoolsengine` rename).
- **Clean fork, diverge freely** — no shared code with the newspaper repo;
  engine fixes won't flow between them. Right call for a solo maintainer.
  See [`ENGINE_EXTRACTION.md`](ENGINE_EXTRACTION.md).
- **Two chunking pipelines** (see [`CHUNKING.md`](CHUNKING.md)):
  - *Narrative* content → embed → topic-trajectory modeling.
  - *Enumerated/consent* content (personnel actions, stipends) →
    **structured extraction** → entity trajectory.
- **Preserve per-person "snooping."** A key intended use is spotting who
  didn't get tenure, who got stipends, etc. So we must **not** merge
  personnel lists into anonymized blobs — the enumerated pipeline keeps
  per-person rows queryable. This directly shaped the two-pipeline split.
- **Embedding is deferred to ingest, not done at scrape time.** Embedding
  will need tuning; coupling it to acquisition would force a re-scrape on
  every tuning change. Scrape once (cheap, polite), embed many times.
- **Allowlist is minimal by intent.** `go.boarddocs.com`, the 8 district
  domains, `*.finalsite.com/.net`, `*.thrillshare.com`. Dropped
  `storage.googleapis.com` (too broad an exfil surface). See
  [`DATA_SOURCES.md`](DATA_SOURCES.md) for the roadmap of future sources
  (meeting-recording transcripts, etc.).

---

## Next steps

1. **Vision-OCR the scanned CBAs** (in progress) — proving on tarrytowns'
   `Tarrytown-TAT-2022-2025.pdf` first (cheapest end-to-end validation of the
   vision→table→extract chain), then white-plains + mount-vernon. Apply
   migration `0005` (bargaining_unit) before extract writes salary rows.
2. **`herald-extract` on the OCR-recovered tables** — land real per-unit
   `salary_schedule` rows; read the `--dry-run` audit flags first (a flood =
   garbled grid → tune `lane_crosswalk.csv`; a handful = real dips to confirm).
3. **Fix the two remaining acquisition gaps:** ossining still returns 0
   contracts (both seeds dead — needs a different source), and a Greenburgh
   download saved as `.bin` PyMuPDF can't open (content-type sniffing at store
   time).
4. **The monthly refresh pipeline** ([`REFRESH.md`](REFRESH.md)) — one
   scheduled, update-only crawl→ingest→OCR→extract workflow, change-detected by
   extracted-text hash so BoardDocs byte-drift doesn't re-ingest duplicates;
   also retires the manual run-id hand-carrying between stages.
5. **Wire the analytical path into `/api/ask`** (web) — it's CLI-only today.
   Then the topic map and the salary/stipend query surface are both phone-usable.
6. **`years_service` over `step`** (STRUCTURED.md decision #6) — prefer
   `years_service` where a contract states it, per-district fallback.
7. **The brief cycle** ([`ROADMAP.md`](ROADMAP.md)) — freeze-and-assign new
   packets to stable topics, seasonal (YoY) drift, the brief (distinct from the
   corpus refresh above: this is the *analysis* cadence, that is *acquisition*).
8. **Budgets (phase 2b)** and re-cluster once table handling has settled; adapt
   Yonkers (non-BoardDocs); investigate thin Greenburgh coverage.

---

## Things to remember for later

- **Database cost:** the corpus will exceed Supabase's 500 MB free tier
  once embeddings land — plan for quantization (as the newspaper engine
  did) and/or a paid tier before the full embed.
- **BoardDocs likes per-IP isolation** — always run one matrix job per
  district; a single runner hitting all 8 gets rate-limited.
- **A district returning 0 docs usually means a new hosting pattern**, not
  a broken crawler. The playbook: fetch its `discovered-<district>.jsonl`
  diagnostic, see where the docs actually live, add a handler. This solved
  Ossining, Port Chester, and the JS-nav cases.
- **pytest-httpx (0.36.2) quirks:** use `url=re.compile(...)` (not
  `url__regex`), `is_reusable=True` for repeated matches, and mock the
  `/sitemap.xml` probe (else unmatched-request teardown failures).
- **Large artifacts don't fit in chat** (Elmsford BoardDocs pull was
  451 MB) — pull them from the Actions artifact instead of attaching.
- **Scrape-time metadata is provisional** — `date`/`doc_type` get their
  authoritative values at ingest.
