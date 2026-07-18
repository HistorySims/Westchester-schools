# Westchester Schools — Project Status

*Last updated: 2026-07-16*

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

**Current milestone:** both scrape sources are now ingested. The
downstream engine (cluster → drift → brief) is the next real gap — the
corpus is large enough to start exploring, but there's no query surface
yet beyond direct SQL.

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

- **~37 scanned PDFs have no text layer (`no_text`) → OCR needed.** These
  aren't random: older Port Chester agendas (2019–2021), several budget
  hearing packets, White Plains budget newsletters — and, most important,
  **two teacher contracts** (Peekskill `PAA CBA 2025-2028`, Mount Vernon
  `MVAG MOA 2022`). Some of the highest-value documents are scanned
  images, so they were skipped. This is the concrete, *targeted* case for
  an OCR pass: we have the exact ~37-document list, no need to OCR the
  corpus. The pipeline records these as `documents.ingest_status='no_text'`
  so they're queryable.
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

1. **Finish the real download** (in progress) — `crawl-sites` with
   `dry_run: false` for the seven strong districts; BoardDocs pulls done.
2. **Investigate Greenburgh** (deferred) — find its real document index /
   sitemap path so it isn't budget-only.
3. **Run the ingest pipeline** — built (2026-07-16): `herald-ingest` +
   the `ingest` workflow consume scrape artifacts by run id (manifest →
   PyMuPDF text → structural chunk → contextual-prefix embed → Postgres,
   schema in `db/migrations/0001_schools_init.sql`; the newspaper chain
   moved to `db/newspaper/`). Dry-run verified on the real Peekskill
   pull: 12 docs → 421 chunks, correct dates (title ▸ document header ▸
   manifest — BoardDocs stamps a school-year-end placeholder on every
   file). Blocked only on secrets: a fresh Supabase project's
   `SUPABASE_DB_URL` + `VOYAGE_API_KEY` in repo Actions secrets, then
   run workflow `ingest` with `init_db` once. The junk/quality filter
   remains deferred (chunks carry a `status` column for it).
4. **Build entity extraction** for the enumerated pipeline —
   `personnel_action` / `contract` tables that preserve per-person
   queryability (tenure, stipends).
5. **Point the inherited engine at the school corpus** — cluster, drift,
   Brief, Dossier. Expect embedding/label tuning.
6. **Adapt Yonkers** (non-BoardDocs) and consider surveying prior art on
   GitHub (many people scrape school-board docs) before over-building.

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
