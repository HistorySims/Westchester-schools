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
- **VERIFIED 2026-08-22.** The re-OCR ran (42 seen, 2 recovered, 118 chunks)
  and Appendix A came back as a real Markdown table:
  `| Step | 2022 - 23 BA | BA15 | BA30 (Frozen) | MA | … | DR |` /
  `| 1 | 63,541 | 66,399 | 69,229 | 70,961 | …` — the pinned step-1 BA value,
  all twelve lanes, school year in the header. The `max_tokens` fix works.
  Next: `extract --reextract` to read it into `salary_schedule`.
- **The same run exposed a new bug.** A `doc_type='contract'` query for
  tarrytowns returned six table chunks and *none* of them the grid — because
  `Tarrytown-TAT-2022-2025.pdf` is typed **`other`**. It matches no keyword,
  and no rule can be expected to know TAT is the Tarrytown Association of
  Teachers. Two causes, both fixed: the ingest-time and scrape-time
  classifiers had drifted apart (`site.py` knew "collective bargaining" and
  "federation of teachers" meant a contract; `classify_doc_type` knew only
  `contract|agreement|mou`), and neither consulted the **URL**, which is
  where the evidence actually was — the file sits under `/contracts/`. The
  vocabulary is now shared, and the URL is a fallback with a deliberately
  *narrower* test: a path segment must end, so `/news/contract-negotiations`
  no longer files a press release as a contract. `herald-extract` was never
  affected — it selects on content keywords, not `doc_type`.
- **Known-good side effect:** the candidate-filter fix took tarrytowns from
  0 → 28 candidate tables and yielded **12 stipend rows** from a personnel
  agenda, clean audit. Board-docs tables were invisible before it.
- **Page-level OCR is now built** (2026-08-21). White Plains' CBA is
  born-digital and full of text, so the document-level `no_text` gate never
  fired — while pages 49 and 58–66, which hold the salary grids, are flat
  images that extract as nothing. The document looked fine and the grids were
  simply absent. `pdf_text.image_only_pages` finds them (little text **and**
  an image covering ≥45% of the page — text alone flags blank dividers,
  images alone flag every letterhead); both OCR engines take a `pages=`
  subset, so a 70-page CBA costs 9 pages of transcription, not 70; and
  `merge_extracted` folds the recovered grids into the born-digital prose
  rather than replacing it. Opt-in via `herald-ingest ocr --partial` and the
  `partial` toggle on the `ocr` workflow, because it costs money and most
  text-bearing documents genuinely need nothing.
- **Still open:** Port Chester's CBA is fully scanned (40 pages, zero text).
  `tarrytownlearningcenter.org` turns out to host contracts for *multiple*
  districts — a better acquisition target than eight separate union sites.

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

There are **two** sources, and between them they cover all eight districts.
Each district uses exactly one:

| District | Source | Policies |
|---|---|---:|
| port-chester-rye | portal `boardpolicyonline.com/?b=port_chester_rye` | **523** |
| ossining | portal `boardpolicyonline.com/?b=ossining` | **434** |
| mount-vernon | BoardDocs console, "Board of Education Policies" | **342** |
| greenburgh-central | BoardDocs console, "…Policy Manual" | **303** |
| peekskill | portal `boardpolicyonline.com/?b=peekskill` | **294** |
| elmsford | portal `boardpolicyonline.com/?b=elmsford` | **249** |
| tarrytowns | BoardDocs console, "Policy Manual" | **242** |
| white-plains | BoardDocs console, "Policy Manual" | **190** |

Ossining and Elmsford have migrated off the legacy MicroScribe Folio infobase
onto the same v3 portal (MicroScribe Publishing Inc runs both brands), so
**one extractor covers all four portal districts**. A slug is confirmed by
response size: a live book returns ~5 KB of SPA shell, an unknown slug returns
37 bytes.

Port Chester's own `/board/policies` page links the manual *and* the loose
PDFs; the site crawler took the PDFs and walked past the manual, because it
follows same-host pages and cross-domain **PDFs** — and a portal is a
cross-domain **HTML app**.

### Solved: how to get a whole manual out (2026-08-20)

`boardpolicyonline.com` is a **Blazor Server** app. There is no REST API to
call — the page is a ~5 KB shell, every byte of policy text is rendered
server-side and pushed down a SignalR WebSocket, and `export.js` / `shared.js`
/ `search.js` / `boot.js` contain **no URL strings at all**. A plain HTTP
client can never see a single policy, which is why `policy-probe` could
characterize the portal but not read it.

What the app *does* expose is its own bulk export. The toolbar's Print button
calls the JS interop function `PrintDocument(html)` with **the entire selected
subtree as one HTML document**. So: check the root node of the table of
contents, click Print, override `window.PrintDocument` to capture instead of
print. Port Chester's whole manual arrives in one round trip — 2.4 MB, 570
sections, 1.6 M characters.

The export is generously structured: one `div.export-section` per policy
carrying `id` (the *same* section id the portal's own deep links use) and
`data-bookmark-title` ("5100 ATTENDANCE"). Every policy therefore gets a
stable, citable `source_url` of `/b/<slug>/s/<id>` — **better provenance than
the loose PDFs we scrape today.**

Two things that had to be discovered by trying:

* The app **hard-depends on jQuery from `code.jquery.com`**. Without it the
  Blazor circuit throws and the page renders only "An unknown error occurred
  while processing your request." jQuery is now vendored in
  `src/herald/scrape/assets/` and served by request interception, so the
  scrape does not depend on a third-party CDN being up.
* Chromium's TLS 1.3 ClientHello is reset by some egress middleboxes that
  `curl` sails through (`net::ERR_CONNECTION_RESET` on *every* host, not just
  the portal). `--tls12` exists for those environments; CI does not need it.

**Built:** `herald.scrape.policy_manual` (browser driver + export splitter),
`herald-scrape policy-fetch`, `.github/workflows/policy-fetch.yml`, and
`herald.html_text` so ingest reads HTML natively instead of round-tripping
policies through a printer. Ingest's 200-char "this PDF is a scan" floor is
**not** applied to HTML — it would have silently dropped 14 short-but-real
policies per manual ("9100 STAFF ETHICS", 143 chars), which is the exact
false-negative failure this work exists to fix.

Verified end-to-end against all four live portals:

```
port-chester-rye  2,418,944 chars -> 570 sections, 523 with policy text
ossining          2,304,689 chars -> 524 sections, 434 with policy text
peekskill         1,918,717 chars -> 328 sections, 294 with policy text
elmsford          1,586,614 chars -> 293 sections, 249 with policy text
```

**1,500 policies** across four districts, against the 13 we held before.

#### In the corpus, live (2026-08-21)

`policy-fetch` run `32416837008` → `ingest` run `32436611943`:

```
seen 1500 | ingested 1500 | skipped 0 | no_text 0 | missing 0 | errors 0 | 5455 chunks
  port-chester-rye 1798 · ossining 1532 · peekskill 1074 · elmsford 1051
```

A clean sweep — every document extracted, chunked and embedded, nothing
lost to a scan or a parse failure. The policy corpus went from **13
documents to 1,500** in one run.
Port Chester's 5100 in the export contains the sentence the corpus was
missing: *"any student with more than nine unexcused ATEDs for one-half year
or 18 unexcused ATEDs for a full year will not receive credit for that
course."*

### Solved: the other four districts, via BoardDocs (2026-08-20)

I had written the other four off — "no portal, only loose PDFs, their manuals
may not be online at all." **That was wrong**, and the error was mine, not the
data's.

The BoardDocs policy console (`BD-GetPolicyBooks` / `BD-GetPolicies` /
`BD-GetPolicyItem`, recovered from `policies.js`) had answered every probe
with the bare string **`No Access`**, which reads exactly like an
authorization wall. It isn't one. It is what BoardDocs returns for a bad
`status` parameter — `""`, `adopted`, `published`, `all` all produce it.
**`status=active` returns the entire manual to an anonymous caller.**

| District | BoardDocs book | Policies |
|---|---|---:|
| mount-vernon | Board of Education Policies | **342** |
| greenburgh-central | Board of Education Policy Manual | **303** |
| tarrytowns | Policy Manual | **242** |
| white-plains | Policy Manual | **190** |

That is **1,077 more policies**, and it makes policy coverage **complete
across all eight districts** — about 2,600 policies against the 13 we held.

This path is in some ways the better one. It needs no browser (plain HTTP
through the existing `BoardDocsClient`), it gives each policy the district's
own permalink `Board.nsf/goto?open&id=<unique>`, and it states **`Adopted`
and `Last Reviewed` as fields** — dates the portal manuals only ever bury in
prose, and exactly what a "when did this change?" question needs.

Two judgement calls worth knowing about:

* Tarrytowns also publishes an **"In Progress"** book (policies mid-amendment)
  and a "Renumbering" book. `choose_policy_books()` takes only the adopted
  manual — answering a parent from a draft would say what the district is
  *considering*, not what binds it. (The draft book is a good future input for
  [BRAIDS](BRAIDS.md), which is about tracking change over time.)
* Ossining answers on **both** paths — but its BoardDocs book holds 12
  policies against the 434 its portal serves. `policy-boarddocs` skips any
  district with a portal by default (`--skip-portal`), so the corpus never
  holds two copies of one policy.

**Built:** policy endpoints + pure parsers on the existing BoardDocs adapter,
`herald-scrape policy-boarddocs`, `.github/workflows/policy-boarddocs.yml`.

#### Two bugs the first live run exposed

A full run stored 1,071 of 1,077 indexed policies. Chasing the missing six
found two real defects, both of the same shape — a silent drop:

1. **Five policies have no inline body at all**; their entire text is an
   *attachment*. Mount Vernon's **0115 (DASA / student harassment and
   bullying)** is one — a substantive policy that would simply have been
   absent. BoardDocs fills those from a separate endpoint that took some
   digging: `BD-GetPublicFiles?open` with `id=<policy unique>` (not
   `parentid`, despite that being the attribute in the item HTML). Both DASA
   PDFs now come down with the policy. 20 policies across the four districts
   carry attachments.
2. **Content-hash dedupe dropped a real policy.** White Plains' 1741
   HOME-SCHOOLED STUDENTS is byte-identical to another policy, so the
   manifest — which dedupes on `sha256` — skipped it, and its number and
   permalink with it. That is right for meeting PDFs (the same file linked
   from two agendas is one document) and wrong for policies: two policies
   with the same text are still two policies. The key is now
   `(source_url, sha256)`, which also means an *amended* policy is no longer
   skipped as already-seen — hash alone would drop the twin, URL alone would
   never notice the amendment.

3. **Word/RTF attachments reached the PDF reader.** A policy's attachment is
   whatever the district uploaded, and six are Word or RTF — White Plains'
   *5700 Purchasing Regulation* (14 KB of text), *5830 Expense
   Reimbursement*, Mount Vernon's *7530-R Child Abuse and Maltreatment*.
   PyMuPDF cannot open any of them, so each was a policy present by title
   with nothing behind it. `herald.office_text` reads both with the standard
   library (docx is a zip of XML; RTF is control words around text).

   Underneath that sat a **filename bug affecting every source**: BoardDocs
   link text reads "Regulation 5830.docx (22 KB)", whose Python suffix is
   `.docx (22 kb)` — so files were stored under names no extractor could
   dispatch on. Attachment filenames now come from the URL, and `RawStore`
   rejects any "extension" that is not a dot plus a few alphanumerics.

Final run: **1,071 policy bodies + 31 attachments = 1,102 documents**, 0
skipped. Four policies are genuinely empty at the source — a title with no
body and no file — and are now *counted and reported* rather than vanishing.
Ingest dry-run over the result: **1,096 ingested, 3,022 chunks**.

#### BoardDocs 403s a datacenter IP (2026-08-21)

The first live `policy-boarddocs` run stored **20 policies out of 1,077** and
still reported success. From this container at 0.35 s intervals the same
scrape pulls all 1,077 without a single refusal; from a GitHub Actions runner
Tarrytowns got 20 through, the next 222 came back `403 Forbidden`, and the
other three districts were refused at the front door — even
`Board.nsf/Public`. It is not the user-agent (already a browser one, and the
first 20 succeeded): it is a rate/volume trip on their WAF against an IP with
no reputation.

Three fixes, in order of how much they matter:

1. **Resume.** Each run restores the previous run's artifact and skips every
   policy already in the manifest *without requesting it*. Re-running a
   cut-off Tarrytowns now takes **4.7 seconds instead of four minutes**, so a
   run that gets blocked is progress rather than a loss — tap Run again.
2. **403 is retryable here, and only here.** `Fetcher` takes
   `retry_statuses`; BoardDocs opts in via `RETRY_STATUSES_WITH_FORBIDDEN`
   with a 5 s base backoff (5/10/20/40/80 s). Everywhere else 403 stays
   terminal, because "you may not" does not improve with patience.
3. **A wiped-out district fails the job.** Per-district errors are still
   caught so one blocked district never costs the other three, but they are
   collected and the command exits non-zero — after ingest and the artifact
   upload, so a partial run keeps everything it did collect.

Default `min_interval` is now 2 s.

#### The cap is on the IP, not the pace (2026-08-21)

Two live runs settled it: **19 policies at `min_interval=2`, 20 at
`min_interval=1`.** Pacing does not move it. BoardDocs allows an anonymous
datacenter IP roughly twenty `BD-GetPolicyItem` calls and then answers 403
for the *whole host* — after Tarrytowns tripped it, `Board.nsf/Public` itself
was refused for Mount Vernon, Greenburgh and White Plains. From an ordinary
connection the same scrape pulls all 1,077 without a single refusal. Resume
would net ~19 a run: **55 taps**, with no guarantee the cap holds still.

There is also no bulk endpoint to fall back on. `policies.js` names only
`BD-GetPolicyBooks` / `BD-GetPolicies` / `BD-GetPolicyItem` (plus editor
calls), and every `PRINT-*` guess a BoardDocs naming convention suggests
(`PRINT-Policies`, `PRINT-PolicyBook`, …) 404s. Unlike the portal manuals,
there is no whole-book export to grab.

**So the acquisition moved off CI.** The manuals were collected from an
unblocked connection and committed as
`data/snapshots/boarddocs-policies.jsonl.gz` — **1,071 policies, 1.7 MB**,
one JSON object per policy carrying district, title, BoardDocs permalink,
fetch time and body HTML. `herald-scrape policy-import` expands it into the
same raw store and manifest a scrape would have left, with the same
`source_url` permalinks, so ingest cannot tell the difference. It is
idempotent, and `data/snapshots/` is the second exception to the "no data in
git" rule after `data/targets/`.

`policy-boarddocs` now takes a **`source`** input: `snapshot` (default, no
network, all 1,071), `boarddocs` (scrape live — for a machine that is not
blocked), or `both`. Verified end to end:

```
policy-import   imported 1071, skipped 0   (re-run: imported 0, skipped 1071)
ingest dry-run  seen 1071 | ingested 1071 | no_text 0 | errors 0 | 2774 chunks
  greenburgh-central 809 · tarrytowns 752 · mount-vernon 669 · white-plains 544
```

### Still open

* **Refreshing the snapshot** needs an unblocked connection — it is not
  something CI can do. The 31 policy attachments (15 MB of PDF/Word) are
  deliberately *not* in the snapshot; they need a separate pass.
* One legacy binary `.doc` attachment (White Plains 5152F) stays unreadable
  — it needs a real converter, and there is exactly one in the corpus. It
  errors rather than silently ingesting as empty.
* A handful of attachment PDFs are scans (`no_text`) — the existing OCR
  pipeline covers them.
* Slug guessing found `irvington` and `dobbs_ferry` are also on the
  BoardPolicyOnline portal — useful if the peer set ever widens.

**Test suite: 271 green.**

---

## Corpus eval — the regression net (2026-08-21)

`herald-eval` + the `eval` workflow ask the corpus questions whose answers are
**known** and check the right passage comes back. See [`EVAL.md`](EVAL.md).

This exists because every failure here has had the same shape — a document
absent or unreadable, and the answer layer reporting the gap as a fact about
the world. The code was correct each time; the tests were green each time.

It grades **retrieval, not prose**: deterministic, no Anthropic key, costs
only the query embeddings. Presence expectations are strong; `no_known_rule`
is recorded but never graded as a required "no", because "no matching sentence
in what we hold" is not "no such rule". A **negative control** fails if any
district returns evidence for a rule that exists nowhere — a suite that only
rewards recall teaches the system to confabulate.

Ten shipped cases, each anchored to a string read out of a document we hold.
The first six test **acquisition** — did the document survive. The later four
test the failure modes a corpus only develops once acquisition works:
**precision** (four districts file the same state device mandate under four
different numbers — 7316, 5695, 5695, 5695 — while every district also has a
staff cell-phone policy sharing the vocabulary, so `must_not_match` fails the
case if the reimbursement policy answers a question about students),
**semantic recall** ("THERAPY DOGS" vs "Use of Assistance Animals"),
**chunking** (one ChatGPT sentence inside a very long Code of Conduct), and
**document choice** (the confiscation procedure lives in 4526.2-R, not the
policy it implements). The original six:
the attendance/credit threshold across three districts, Mount Vernon's
attachment-only DASA policy, White Plains' identical-twin 1741, its Word-only
purchasing regulation, the Tarrytowns salary grid (**expected to fail until
the re-OCR runs** — it names the pending work rather than hiding it), and the
negative control.

---

## What "pay Google last year" revealed (2026-08-23)

A live question — *"How much did each district pay Google last year?"* —
produced an answer that was **right to refuse**:

> *"This means the corpus as searched does not surface a Google-specific
> expenditure — it does not mean the districts did not pay Google last year."*

That paragraph is the epistemics this project has been building toward, and it
is now guarded by an eval case. But the run exposed two real problems.

**"last year" was silently dropped.** `--since` was empty and nothing read the
question, so a time-scoped question was answered from evidence dated **2013 to
2026**. `herald.timeframe` now reads the question: resolvable phrases ("in
2024", "the 2023-24 school year", "since 2022") become a date filter, and
**vague ones are reported, never guessed**. "Last year" in August could be the
2025 calendar year or the 2025-26 school year, and district documents run on
the latter — so the answer now carries a line, directly under the heading,
saying it was not scoped and what the evidence actually spans. A school year
is July–June, which is why "2023-24" is not the window "2023".

**The question is unanswerable by document class, not by coverage.** All 32
retrieved passages were budget books, topically perfect and structurally
incapable of naming a vendor: budgets appropriate by *function code*
("CONTRACT SRV NETWORK", "BOCES SERVICES"), never by payee. Vendor payments
live in the monthly **warrant / check register** the board approves and in
claims-auditor reports — a document class we do not acquire at all. Those are
usually BoardDocs agenda attachments, so they are probably reachable.

That distinction is worth keeping straight: a coverage gap is fixed by
crawling harder, a document-class gap is not fixed at all until someone
notices the answer can never be in the documents being searched.

---

## The 908 `other` documents (2026-08-23)

A reclassify pass moved only 8 of 908, so we looked at what the 908 actually
are rather than guessing. Grouped by the first word of the title:

| first word | docs | districts | example |
|---|---:|---:|---|
| min | **90** | 1 | `Min 1.11.24.pdf` |
| boe | 51 | 3 | `BoE Presentation - Cultural Arts` |
| treasurer's | 43 | 1 | `Treasurer's Report April 2023.pdf` |
| fixed | 25 | 1 | `Fixed Asset Disposal - 20 Chromebook Carts.pdf` |
| charters / charter / club | ~33 | 1 | `Club Charters 11-07-24.pdf` |

Two things fall out of that shape.

**Ninety of them are minutes.** BoardDocs names the attachment
`Min 4.18.23.pdf`, and "min" is not "minute", so the whole Tarrytowns meeting
record was unfilterable. Fixed with an anchored rule that requires the date
which follows — a bare `min` prefix would swallow every *minimum*,
*mini-grant* and *minority* report in the corpus, and there is a test for
each direction.

**Nearly every group is one district.** This is Tarrytowns' BoardDocs
attachment sprawl (6,558 chunks — it backs up nearly every consent-agenda
item as its own file), not a corpus-wide problem. Worth remembering before
reading `other` as a quality signal.

**And 898 of the 900 come from BoardDocs** — so this was never really a
classifier problem. Fetching a live agenda showed the label was there all
along and being discarded: attachments sit in `div.print-files` inside
`div.container.item.agendaorder`, and the item's own heading says plainly
what the file is.

| file | agenda item |
|---|---|
| `Min Org 7.9.26.pdf` | Minutes Reorganization and Regular Meeting July 9, 2026 |
| `Draft Policy 8636 - Artificial Intelligence.pdf` | Policy 8636 Artificial Intelligence (AI) |
| `2026-2027 Mid-Westchester Consortium…` | Special Education **Agreement** |

`FileRef` now carries `item_title`, and it becomes the document's title as
well as a classification signal. On the sample agenda that retyped 1 of 15
files — `Min Org 7.9.26.pdf` defeats even the abbreviation rule, since "min"
is followed by "org" rather than a date — but it improved the **title of all
15**, which is the bigger win: a citation reading "Minutes Reorganization and
Regular Meeting July 9, 2026" beats one reading "Min Org 7.9.26.pdf
(3,798 KB)". Many of the 898 are genuinely miscellaneous (club charters,
asset disposals) and will stay `other`, correctly.

This only affects **future** BoardDocs crawls — existing documents keep the
titles they were ingested with. A re-crawl is what would apply it.

### An open taxonomy question

`treasurer's` (43) is a real, recurring document class with nowhere to go.
A Treasurer's Report is a **financial statement**, not a spending plan, and
`DocType` has only `budget`. Folding it in would mean "show me the budget"
returns monthly cash reports — the same reason audits were deliberately
excluded. The honest options are a new `financial` type (treasurer's reports,
audits, and the warrant registers we do not yet acquire) or leaving them
`other` and accepting they cannot be filtered to. That is a schema decision,
not a classifier one, so it is recorded here rather than guessed at.

**Decided 2026-08-24** — both new types exist:
`0006_doc_type_presentation_financial.sql`.

---

## The board-minutes hole (2026-08-28)

Measured against NY Public Officers Law § 106, which requires minutes within
two weeks of a meeting — so every meeting that produced an agenda must have
produced minutes. This is the first **statutory** absence test in the project:
unlike `data/eval/schools_cases.json`, whose every expectation was read out of
a document we already hold, it can detect a document that is missing.

Coverage of `doc_type in ('agenda','minutes')`, by district:

| district | agendas | minutes | minutes span |
|---|---:|---:|---|
| port-chester-rye | 37 | 149 | 2011-07-05 → **2020-06-15** |
| tarrytowns | 188 | 78 | 2022-12-15 → 2026-06-30 |
| ossining | 16 | 90 | 2023-07-03 → 2026-05-19 |
| elmsford | 7 | 88 | 2023-01-11 → 2026-06-29 |
| white-plains | 22 | 27 | 2022-12-05 → 2026-06-01 |
| greenburgh-central | 0 | 4 | 2026-06-02 → 2026-06-26 |
| peekskill | 12 | 0 | — |
| mount-vernon | 0 | 0 | — |

*A snapshot, kept because it is what prompted the diagnosis. The counts moved
the same week — see the two sections below, which supersede it.*

Three findings, in order of how much they cost us:

**1. A corpus-wide hole from 2020-06-15 to 2022-12-05.** Port Chester's
minutes stop in June 2020; every other district's begin in December 2022 or
later. For those thirty months **no district in the corpus has a single set of
board minutes** — which is exactly the period covering school closure,
reopening, masking and remote learning. The most consequential board decisions
these districts have made in a generation are the ones we cannot see.

**2. Three districts have no usable meeting record at all.** Mount Vernon has
418 ingested documents and not one agenda or minute. Peekskill has twelve
agendas, all from 2026, and no minutes. Greenburgh has four minutes, all from
June 2026. Any question of the form "what did the board decide" is
unanswerable for three of eight districts, and nothing in the system knows to
say so — it returns nothing, indistinguishable from a district where nothing
happened.

**3. Port Chester is inverted.** The anchor district's agendas (2018-2025) and
minutes (2011-2020) barely overlap. Its 32 "agenda without minutes" dates are
not scattered holes but one clean cutoff: nothing after June 2020.

### What this cost, methodologically

The probe that found this — "meetings with an agenda and no minutes" — could
only see districts that *have* agendas, so the three districts in the worst
shape were invisible to it by construction. The same blind spot as the eval
suite, one level up: a test that measures the quality of what we hold rather
than the extent of what we do not. The fix was a second query that counted
both classes per district and asked nothing about matching.

### Why each district is missing what it is missing (2026-09-05)

Diagnosed by reading the districts' live BoardDocs rather than inferring from
the corpus. Three different causes, and only one of them is a crawl problem:

**Peekskill — mislabeled, not missing.** Its whole archive is a pseudo-meeting
named `2026 Minutes`, dated 2026-12-31, holding one attachment per meeting of
the year: `Subject B. Business Meeting July 28, 2026`. Each attachment says
"business meeting" and never "minutes", so `classify_filename` returned
`other` and the ingest classifier upgraded it to `agenda`. 16 agendas, zero
minutes, with the minutes sitting right there. Fixed in `iter_documents`
(the parent meeting's name now types its attachments); applies to future
crawls, and Peekskill has 487 meetings, so earlier years should follow.

**Mount Vernon — purely the 403 wall.** 377 meetings; its 2026-08-25 Business
Meeting alone carries 46 attachments. We hold 19 documents for the district.
Nothing structural, just barely started. Repeated resumable passes are the
whole fix.

**Port Chester — not published where we can reach it.** The anchor district,
and the worst answer of the three. Checked directly:

* BoardDocs holds **139 meetings, 2021-10-28 → 2026-08-27**. Nothing earlier.
* **No meeting is named "minutes"** — no collection scheme like Peekskill's.
* Its meeting record offers three buttons: *View the Agenda*, *Download
  Agenda as PDF*, *Print the Agenda*. **There is no minutes view.**
* The newest meeting's 32 attachments are all agenda support — donations,
  affiliation agreements, conference approvals. None are minutes.
* Its agenda HTML contains **zero occurrences of the word "minutes"** in
  133,751 bytes — not even an "Approval of Minutes" item.
* There is only one committee, so that is all of its BoardDocs.
* The district's own archive page says materials from 2021-22 onward are "on
  BoardDocs", which is where they are not.

Our 149 Port Chester minutes (2011-07-05 → 2020-06-15) came from the website
archive, and BoardDocs begins in October 2021. So minutes from roughly
mid-2020 onward are not published in any of the three places the district
itself points to.

That is an evidenced absence, not a proven one — it says we could not find
them, and Public Officers Law § 106 says they should exist. Confirming it
means asking the district or filing a FOIL request, which is a human errand
and the next step for this district specifically.

### The fix: the agenda is the board record (2026-09-05)

Asking *"could we just use the agendas?"* found a larger bug than it was
aimed at. The crawler fetched every agenda, scraped the attachment links out
of it, and **discarded the body** — roughly 19,500 characters of itemised
meeting content per meeting. That, not the 403 wall and not classification,
is why Mount Vernon and Greenburgh showed zero agendas across hundreds of
meetings: no agenda was ever saved as a document.

`iter_documents` now yields the agenda itself. BoardDocs serves the rendered
agenda over GET as well as the POST the crawl uses (verified against pcru:
322,136 bytes, byte-identical text), so it downloads, hashes, dedupes and
resumes like any other artifact. Saved as `.html` so ingest dispatches to
`extract_html` rather than PyMuPDF, which would read it as nothing and file
it `no_text`. Minutes-collection meetings are skipped — their "agenda" is an
index of attachments, and saving it would invent a meeting that never
happened.

**What an agenda can and cannot support.** It says what was *proposed*. It
carries no outcome: Port Chester's agendas contain zero occurrences of
motion, carried, ayes, nays, vote, unanimous or tabled. So "the board took up
X" is sourceable from an agenda and **"the board approved X" is not**. That
distinction has to survive into whatever writes the newsletter.

**Deferred: meeting video.** Port Chester hosts recordings on Panopto, and a
caption track would be the only artifact we have found that records what was
actually decided. `herald-scrape panopto-probe` and the `panopto-probe`
workflow exist to find out whether captions are readable without a login (a
restricted folder answers 200 with an *empty* body, so status alone proves
nothing). Deliberately not pursued for now — agendas are enough for the
current goals, and a transcript is not minutes: speech recognition mangles
proper nouns, resolution numbers and dollar amounts, and a three-hour meeting
is ~30,000 words of largely procedural talk.

### Scope statement

What this corpus can be trusted to answer, as of 2026-09-05:

* **Policies and regulations** — all eight districts, good coverage, and
  current by construction (scraped from the live published manuals).
* **Contracts** — all eight present. **Currency unverified**: the CBA used as
  this project's test case, `Tarrytown-TAT-2022-2025`, expired over a year
  ago. "Compare current contracts" may be comparing superseded agreements
  while sounding authoritative. Unresolved.
* **Budgets** — all eight; materially cleaner since 226 slide decks moved to
  `presentation` and stopped competing with the budget books.
* **What boards took up** — all eight, once the agenda-capture crawl has run
  its passes. This is the newsletter's raw material, not the newsletter: it
  still has to go through the quality gate, the topic map and drift before it
  says anything. See Next steps.
* **What boards decided** — four districts (Elmsford, Ossining, Tarrytowns,
  White Plains), from ~December 2022. Peekskill's archive is recoverable and
  mislabeled rather than missing. Port Chester's is not published anywhere we
  can reach; Mount Vernon's and Greenburgh's depend on the crawl advancing.
* **Vendor payments** — no. Budgets appropriate by function code, never by
  payee; warrant registers are still unacquired. The corpus correctly
  refuses, and `unanswerable-by-document-class` guards that.

### How to run the acquisition loop (2026-09-05)

**BoardDocs blocks by IP, not by request shape.** Verified: a file that 403s
on a runner returns 200 and a 109 KB PDF from another address, with identical
headers, and a full navigation header set (`Sec-Fetch-Dest: document`,
`Referer`) behaves the same as the current one. Pacing does not move it and
header tuning will not either. One runner per district (`scrape-all`,
`refresh`) is the mitigation: each district gets its own IP.

**The 403 was never the real problem — non-resumption was.** `Manifest`
already skips what it holds, but a runner checks out a clean tree with no
manifest, so every run re-downloaded the same first files, hit the wall in
the same place, and never advanced. `.github/actions/seed-manifest` restores
the newest `scrape-<slug>` artifact's manifest first, which is what makes
repeated runs additive.

Three rules follow, and getting them wrong wastes passes:

1. **Use `refresh`, not `scrape-all`, for repeated passes.** The seed
   restores the *manifest*, not the files. Run 3's artifact lists everything
   from runs 1-3 but holds only run 3's files on disk, so ingesting once at
   the end loses the earlier runs' documents — they resolve to no local path
   and are marked `error`. `refresh` crawls and ingests in one run, on that
   run's own artifacts, so each pass is banked before the next starts. Pass
   `since` explicitly for a backfill; blank uses the rolling window.
2. **Serial, never concurrent.** Both workflows share the `crawl` concurrency
   group, so a second dispatch queues. That is not only politeness:
   concurrent runs seed from the same completed artifact, so they would go
   after the *same* next slice and duplicate each other rather than divide
   the work.
3. **Watch `resuming: N document(s) already recorded` in each district's
   job.** N must climb every pass. If it is flat, the seeding is broken and
   more passes are pointless.

**Staying current is a separate problem from backfilling.** Every workflow
was `workflow_dispatch` only, so the corpus was only ever as fresh as the
last time someone tapped a button — on 2026-09-05 its newest board document
was dated July 1st, two months stale in every district including the healthy
ones. `refresh` runs weekly on a cron. Two things silently disarm it:
scheduled triggers **only fire from the default branch**, and GitHub disables
scheduled workflows in a repository with no commits for 60 days.

---

## `presentation` and `financial` (2026-08-24)

**What forced it.** Chasing an OCR bill, a per-page query surfaced 108
documents averaging 40–250 characters per page. They are not scans — they are
**board slide decks**. Ossining's "Directors' Budget Presentation" is 56 pages
of which `image_only_pages` flags 46 as flat images; its extractable text is
promotional ("Strengthening Our Digital Infrastructure"), and the classifier
typed it **`budget`** — the same type as the actual budget books.

That is a live retrieval defect with no OCR involved. A question about a
spending line can retrieve a slide reading *"An increase in teaching
salaries"* next to an arrow icon, instead of the budget-book line carrying the
figure. Topically perfect, structurally incapable of answering — precisely the
failure `unanswerable-by-document-class` exists to catch.

**And it was nearly made much worse.** The plan under discussion was a ~$45
vision-OCR pass over exactly these documents. That would have injected 46
pages *per deck* of fluent budget-vocabulary prose with no figures in it,
directly competing with the budget books. The pass was cancelled: OCR here
buys pollution, not recall.

**The exception, recorded for later.** Two slides in that deck are not
advertising — "Budgetary Changes (Budget Book)" cites the budget book's own
page numbers and gives the *reason* a line moved ("Shifts in budget codes —
from Consultants to BOCES"; special-ed placements down, teaching salaries up).
The budget book gives the number and never the why. Same shape as the vendor
question: a fact the primary document class structurally cannot hold. Not
worth OCR at corpus scale; worth knowing it exists.

**Ordering is the whole trick** (`classify_doc_type`):

* `presentation` is tested **before** the meeting keywords, or
  "OUFSD - BOE Meeting Presentation" matches `boe meeting` and lands in
  `agenda`; and **before** `budget`, or "Directors' Budget Presentation"
  lands in `budget`.
* `financial` is tested **after** `agenda`/`minutes`, so an "Audit Committee
  Agenda" stays an agenda and "Audit Minutes" stays minutes — and before
  `budget`, so a Treasurer's Report is not read as a spending plan.
* `audit` cannot reach "auditorium" (verified: "Auditorium Use Agreement"
  → `contract`).

Genre, not subject: what makes a document a presentation is that it was built
to be *shown*, whatever it is about. The corpus previously could not tell
persuasion from record — which is also why "what is each board highlighting?"
was unanswerable. It is now a filter.

**To repair the corpus in place**, `reclassify` must be run with
`--from-type budget` (the workflow now exposes `reclassify_from`); the default
`other` will not move a deck already typed `budget`.

---

## Migrations are now runnable from a phone (2026-08-24)

`0005_bargaining_unit.sql` sat unapplied for weeks while the code that needed
it was already merged. Not a decision — applying it needed `psql` on a laptop,
and the only available device was a phone. A migration that cannot be run is
not shipped.

`herald-migrate` (+ the `migrate` workflow) applies `db/migrations/*.sql` in
filename order, one transaction each, recording every file in
`schema_migrations` in that same transaction. Re-running is safe; a file whose
text changed after it was applied is **reported and never re-run**, because
silently re-executing edited DDL is how a schema drifts from what the
migration files claim.

**Expect the first run to list 0001–0006 as pending** — `schema_migrations`
starts empty and knows nothing of what was applied by hand. All of 0001–0004
are written idempotently (`if not exists`, and 0002's constraint sits behind a
`pg_constraint` guard), so re-running them is a no-op that simply records
reality. Run the dry run first and read the list.

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

Ordered against the two stated goals: **(A)** search and compare *current*
policies and contracts, **(B)** a monthly newsletter of what the boards are
doing.

**Goal B is a pipeline, and every stage of it already exists.** Acquisition
is only the first stage — it is a prerequisite for the newsletter, not the
newsletter. The stages, in dependency order:

> **acquire** (`refresh`) → **quality-gate** (`score`) → **topic map**
> (`cluster`) → **what changed** (`cluster-drift`) → **the brief**

`cluster` discovers topics rather than imposing them (UMAP → HDBSCAN → model
labels), which is what lets a newsletter say "these four districts are all
arguing about the same thing" without anyone having defined that thing in
advance. `cluster-drift` bins each cluster's chunks by ISO week and measures
centroid movement — that is the lead-finder: a topic whose centroid jumps
this month is the thing worth writing about. Neither is optional to goal B.

1. **Run the `refresh` backfill passes** — `since: 2023-01-01`, serially, one
   at a time, watching `resuming: N` climb. See "How to run the acquisition
   loop" above; using `scrape-all` instead, or running passes concurrently,
   both waste them. Six of eight districts need this before the rest of the
   pipeline has anything to work on.
2. **Re-run `score` once the agendas land** — this matters *more* now, not
   less. Agenda capture is about to add thousands of documents that are
   largely procedural ("Approval of Conference(s)" eight times in one
   meeting), and the quality gate exists to quarantine exactly that. Both the
   topic map and Ask filter `status='active'`, so scoring cleans both at
   once, and it is reversible.
3. **Re-cluster, then re-run `cluster-drift`** — the corpus is about to
   change shape substantially. Both need to run on the new corpus before any
   drift number means anything; drift measured across an acquisition jump
   reports the crawl, not the boards.
4. **Check contract currency** — goal A's one unverified assumption.
   `Tarrytown-TAT-2022-2025` expired over a year ago. If every district's
   CBAs predate 2025, "compare current contracts" is comparing superseded
   agreements while sounding authoritative, which is worse than answering
   nothing. Query in the scope statement above.
5. **Arm the schedule** — `refresh`'s cron is inert until the workflow is on
   the default branch. Without it the corpus goes stale between manual
   dispatches, which is fatal for a *monthly* product.
6. **The brief cycle** ([`ROADMAP.md`](ROADMAP.md)) — freeze-and-assign new
   packets to stable topics, seasonal (YoY) drift, the brief itself. This is
   the newsletter, and items 1-3 are what feed it.
7. **The validation exercise** — questions supplied by the operator (not by
   the model, which biases toward what it knows works), graded against the
   statute-derived list in `data/targets/required_documents.json`. The only
   test that can detect a *missing* document; `data/eval/schools_cases.json`
   cannot, by construction.
8. **Wire the analytical path into `/api/ask`** (web) — it's CLI-only today.
   Then the topic map and the salary/stipend query surface are both
   phone-usable, which the newsletter cycle needs.
9. **`herald-extract` on the OCR-recovered tables** — land real per-unit
   `salary_schedule` rows; read the `--dry-run` audit flags first (a flood =
   garbled grid → tune `lane_crosswalk.csv`; a handful = real dips to confirm).
10. **Fix the remaining acquisition gaps:** ossining still returns 0 contracts
    (both seeds dead — needs a different source), and a Greenburgh download
    saved as `.bin` PyMuPDF can't open (content-type sniffing at store time).
11. **`years_service` over `step`** (STRUCTURED.md decision #6) — prefer
    `years_service` where a contract states it, per-district fallback.
12. **Deferred by decision, not oversight:** meeting-video captions (Panopto,
    and Ossining's YouTube archive) — the only source that records what was
    actually decided; probe built, not run. Warrant registers — the only
    source that names vendors. Both answer questions the corpus currently and
    correctly refuses.
13. **Budgets (phase 2b)**; adapt Yonkers (non-BoardDocs).

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
