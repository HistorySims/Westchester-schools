# The monthly refresh pipeline (planned)

*Status: design. Some stages exist and are incremental already; the
orchestration + change-detection pieces below are the new build.*

The corpus is a snapshot. Districts post new agendas, minutes, budgets and
(occasionally) new contracts every month, and amend existing policies. We
want one **scheduled monthly job** that walks every source, pulls in only
what is **new or changed**, and runs it all the way through to structured
salary/stipend rows — without re-embedding the whole corpus, re-OCRing
unchanged scans, or re-extracting settled tables, and **without the
operator copying run-ids between workflows.**

## The whole chain in one workflow

Today acquisition → ingest → OCR → extract are four separate
`workflow_dispatch` workflows, and the operator hand-carries a `run_id`
from each stage to the next (the single most error-prone, phone-hostile
step — we already tripped over a dry-run `run_id` and a stale one). The
refresh tool collapses them into **one scheduled workflow with chained
jobs**, where each job consumes the previous job's artifacts *within the
same run* (`needs:` + `upload/download-artifact`, no run-id at all):

```
monthly-refresh.yml   (cron: 1st of month, + manual dispatch)

  crawl   (matrix ×N sources/districts, --since <watermark>)
    │  uploads data/raw/** + manifest.jsonl per district
    ▼
  ingest  (needs: crawl)   new + changed docs only → prose/table chunks
    │
    ▼
  ocr     (needs: crawl)   new no_text docs only → vision for contracts
    │
    ▼
  extract (needs: ingest)  new table chunks only → salary/stipend rows
    │
    ▼
  report  (needs: all)     one "what changed this month" summary
```

Cron gives the cadence; `concurrency: monthly-refresh` prevents overlap;
a manual dispatch with the same inputs lets us run it on demand.

## "Only seek updates" — the identity + change model

Incremental means every stage must cheaply answer *"have I already
processed this, unchanged?"* The unit of identity is **(district,
source_url)** — already stored on `documents`. What's missing is reliable
**change detection**, because raw bytes are not a stable identity:
BoardDocs re-renders the same agenda to different bytes on every fetch
(we confirmed the sha256 drift), so a raw-byte hash reports phantom
"changes" and would re-ingest everything monthly.

**Fix: hash the extracted *text*, not the bytes.** Add
`documents.text_sha256` (sha256 of the normalized extracted text). Then
each crawled doc falls into exactly one bucket at ingest:

| source_url seen? | text hash | action |
|---|---|---|
| no | — | **NEW** → ingest (prose + tables), embed, extract |
| yes | same | **UNCHANGED** → skip (no embed, no extract) |
| yes | differs | **UPDATED** → re-ingest in place (see below) |

This makes "only updates" real: BoardDocs byte-drift → same text hash →
skipped; a genuinely amended policy → different text hash → re-ingested.

### Updated documents (re-ingest in place)

An amended doc keeps its `document_id` and `source_url`; the refresh
**replaces** its chunks (delete the doc's chunks, insert the new ones,
re-embed), bumps `documents.revision` and `updated_at`, and clears
`extracted_at` on its table chunks so extract re-reads them. v1 keeps only
the current version (history lives in git-of-content nowhere; we don't need
diffs yet). The salary/stipend upserts are already idempotent on their
unique keys, so a replaced grid corrects rather than duplicates.

## Per-stage incrementality (what exists vs. what's new)

- **Crawl.** *New:* a `--since <date>` watermark so heavy sources only pull
  recent material. BoardDocs' meeting list is dated — crawl only meetings
  after the last refresh instead of re-walking years of history every
  month (the single biggest cost saving). District-site crawls stay full
  (cheap) but their downloads dedupe downstream.
- **Ingest.** *Exists:* `herald-ingest run` already skips already-ingested
  docs by content hash. *New:* switch the skip test from raw-byte hash to
  `text_sha256`, and add the UPDATED path above.
- **OCR.** *Exists:* `ocr_mode` acts only on `no_text` docs, and a recovered
  doc flips to `ingested`, so it is never re-OCR'd. Each month only *new*
  scans are processed. *New:* a cost gate — vision (paid) only for
  contract-type docs; tesseract or skip for scanned budget backups — so the
  monthly Claude spend stays bounded to genuinely new contracts.
- **Extract.** *Exists:* `herald-extract --only-new` already processes only
  table chunks with `extracted_at is null`. Nothing new needed; the UPDATED
  path clears `extracted_at` so amended grids re-extract.

## OCR engine — open question (not yet decided)

The current vision engine is Claude Sonnet-5, chosen because the pipeline is
already Anthropic-native and the one-time contract backfill is only a few
dollars. **If OCR becomes a recurring monthly cost, revisit the engine** —
at volume there are cheaper and/or better-fit options worth a real bake-off,
and the field is moving fast enough that the shortlist will have grown by
then. Early candidates noted (2026-08, *not* exhaustive): **Gemini 3.7 Flash**
(cheap VLM, strong tables), **Mistral OCR** (dedicated doc-OCR, flat per-page
~$1–2/1k, markdown+table output), and **PaddleOCR PP-Structure** (open,
CPU-runnable → free on Actions, unlike GPU-bound VLMs). The `ocr.py` engine is
already pluggable (`--engine`), so adding one is bounded. Decision rule when
we get there: bake off the top few on the *same* salary-grid pages and judge
on **digit accuracy against the source**, not leaderboard rank — a transposed
salary figure is a silent corruption. No action now; flagged for when the
monthly refresh makes OCR a standing line item.

## The watermark

A small `corpus_refresh` table records, per (source, district), the
`last_run_at`, and per-run counters (docs added / updated / skipped, chunks
added, rows added, OCR recoveries, errors). The crawl reads it for
`--since`; the report writes the new row. Keeping state in the DB (not a
committed repo file) means the workflow needs no write-back to git and no
extra permissions.

## What the operator sees

One monthly artifact + run-summary: a per-district table of **added /
updated / unchanged / no_text / errors** and **new salary/stipend rows**,
i.e. *what changed this month* — the same shape as today's per-stage
reports, consolidated. On a quiet month it reads "0 new, 0 changed" and
costs almost nothing (crawl + hash checks, no embed/OCR/extract).

## Build order

1. `documents.text_sha256` + the UNCHANGED/UPDATED/NEW switch in ingest
   (migration + `ingest_schools`), with the update-in-place chunk replace.
2. `corpus_refresh` watermark table + `--since` on the BoardDocs adapter.
3. `monthly-refresh.yml` — chained jobs, artifacts via `needs`, `cron`.
4. Vision-OCR cost gate (doc_type filter) + the consolidated report.

Stages 1–2 are the substance ("only updates"); 3 is orchestration that
also retires the run-id hand-carrying; 4 is cost hygiene. Until this
lands, the manual four-workflow chain remains the way to refresh.
