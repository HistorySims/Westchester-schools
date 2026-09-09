# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A semantic-research corpus of **Westchester County, NY public-school-district
governance documents** — board agendas, minutes, policy manuals, handbooks,
teacher contracts, budgets — across eight peer districts. Two goals:

- **A**: search and compare *current* policies and contracts
- **B**: a monthly newsletter-style overview of what the boards are doing

Note the README is stale: it describes **Herald**, the 1840s-newspaper engine
this repo was forked from. The package is still named `herald` and stays that
way. `docs/STATUS.md` is the accurate picture; `docs/HERALD_NEWSPAPER_STATUS.md`
covers the roots.

## Commands

```bash
uv sync --frozen              # install (CI uses --frozen; plain `uv sync` locally)
uv run pytest -q              # tests
uv run pytest tests/test_html_text.py::test_split_header_and_body_tables_are_rejoined
uv run ruff check src tests   # lint — CI lints ONLY src and tests, not scripts/
uv run pyright                # type check
```

`uv run ruff check .` reports pre-existing failures in `scripts/`, which CI does
not lint. Use `src tests` to match CI.

Console entry points (`pyproject.toml [project.scripts]`): `herald-scrape`,
`herald-ingest`, `herald-ask`, `herald-cluster`, `herald-score`,
`herald-extract`, `herald-eval`, `herald-migrate`. Each is a Typer app; `--help`
on any subcommand is generally accurate and worth reading before guessing.

Optional dependency groups, deliberately not runtime deps:
`uv sync --group analysis` (bertopic), `--group browser` (playwright, for the
Blazor policy portals whose text only exists after client render).

## The fork boundary — read this before editing

Modules named `*_schools.py` / `schools_*.py` belong to **this** project.
Bare-named modules are **inherited newspaper-engine code targeting a different
database with a different schema** (`pages`, `issues`, `is_current`). Several
are marked `FORK TODO` at the top. `pyproject.toml`'s per-file lint ignores list
them; the list is not to be grown.

| this project | inherited (do not edit for schools work) |
|---|---|
| `schools_retrieval.py` | `retrieval.py`, `db.py` |
| `chunking.py` (BoardDocs outline) | `chunker.py` (fixed-window) |
| `cluster_schools.py` | `cluster.py` |
| `ingest_schools.py`, `ask_schools.py`, `extract_schools.py` | `synth.py`, `models.py`, `classify.py` |

The authoritative list of not-yet-rewritten modules is
`[tool.ruff.lint.per-file-ignores]` in `pyproject.toml`: `classify.py`,
`cluster.py`, `models.py`, `quality.py`. `quality.py` is in that list but does
write the schools `chunks` table — check what a function touches before assuming
which side of the fork it is on.

Shared and safe: `embed.py`, `rerank.py`, `pdf_text.py`, `html_text.py`,
`office_text.py`, `ocr.py`, `taxonomy.py`, `timeframe.py`.

`schools_db.py` re-exports `connect` from `db.py` — that connection helper is
shared even though the schema is not. A change that looks like it belongs in
both places usually belongs in only one; check which database the SQL targets.

## Pipeline

```
scrape → raw files + manifest.jsonl → ingest (extract → chunk → embed) → Postgres
                                                                            ↓
                              score (quarantine) → cluster → drift → ask / brief
                                                                            ↓
                                          extract → salary_schedule / stipend_schedule
```

**Acquisition** (`src/herald/scrape/`) drives per-source adapters —
`boarddocs.py` is the main one, plus `policy.py`, `policy_manual.py`, `site.py`,
`panopto.py`. `runner.py` orchestrates and writes an append-only
`manifest.jsonl` next to the files. The manifest is the contract between scrape
and ingest, and it doubles as the dedupe key: a `source_url` already in it is
never refetched.

**Ingest** (`ingest_schools.py`) reads a manifest, extracts text by file type
(`pdf_text` / `html_text` / `office_text`, with `ocr.py` for scans), chunks
prose on the agenda outline plus one whole-table chunk per detected grid, embeds
with a deterministic contextual breadcrumb (`embed_input`), and writes documents
+ chunks in one transaction so re-runs resume cleanly.

**Retrieval** (`schools_retrieval.py`, `ask_schools.py`) is hybrid: vector
distance plus FTS, per-district panels, then rerank and synthesis.
`analytical.py` intercepts questions answerable from the structured tables
(salary at a lane/step) rather than from prose.

### Schema notes that bite

- `chunks.embedding` is **`halfvec(1024)`** as of migration 0007, not `vector`.
  There is no `halfvec <=> vector` operator, so query vectors must be cast
  `::halfvec(1024)`. `db.py` still casts `::vector` — correct, different database.
- **There is no HNSW index.** It was 366 MB of a 500 MB tier; vector search is a
  sequential scan and fast enough at this corpus size. See `docs/DISK_SPACE.md`.
- `chunks.fts` is a `generated always as stored` tsvector; the FTS and HNSW
  indexes are **partial** on `status = 'active'`, so quarantined chunks are
  invisible to both.
- `insert_chunks` is `on conflict (document_id, chunk_index) do nothing`. Any
  backfill that re-derives existing chunks must delete them first or it silently
  changes nothing — that is what `herald-ingest tables --replace` is for.
- `n_live_tup` in `pg_stat_user_tables` reads 0 for never-analyzed tables. Not
  the same as empty; use `count(*)`.

## Migrations

`db/migrations/NNNN_*.sql`, applied in filename order by `herald-migrate apply`,
one transaction each, recorded in `schema_migrations`. A file whose text changed
after being applied is reported and never re-run.

**Write every migration idempotently** — guard with `do $$ ... pg_constraint`
blocks. Several were applied by hand before `schema_migrations` existed, so the
runner has no record of them and will try again.

## Operating constraints

These shape most design decisions here and are not obvious from the code:

- **The user works from a phone.** Anything that needs running must be a
  `workflow_dispatch` workflow under `.github/workflows/`. Roughly 25 exist; a
  local-only command is not a deliverable.
- **Supabase is not reachable from the dev container.** Workflows on GitHub
  runners reach it fine; you cannot. Hand SQL to the user and ask them to paste
  results back rather than debugging the connection.
- **BoardDocs blocks by IP range, not request count.** GitHub runners get 403
  after a handful of fetches while an ordinary connection does hundreds. This is
  why `data/snapshots/*.jsonl.gz` exists: collect where the network works, commit
  the result, expand it on the runner with `*-snapshot-import`. Do not try to
  pace, retry or re-header around it — all measured, none work.
- **Scheduled workflows only fire from the default branch**, and are disabled
  after 60 days without commits.

## Conventions

- Comments explain *why*, especially when the code encodes a hard-won fact — a
  measured limit, a bug that recurred, a source's quirk. Match that density;
  this codebase is unusually comment-heavy on purpose.
- Ruff line length 100, `select = ["E","F","I","B","UP","SIM","RUF"]`. RUF001/2
  (ambiguous unicode) fire on curly quotes and en-dashes pasted from documents.
- Be honest about absent data. "No schedule extracted for X" is a correct answer
  and better than a plausible number; `ask` is built to say so.

## Docs worth reading before large changes

`docs/STATUS.md` (current state), `docs/CHUNKING.md` (chunk + schema design),
`docs/STRUCTURED.md` (salary/stipend extraction), `docs/SCRAPING.md`,
`docs/ENGINE_EXTRACTION.md` (the fork boundary above), `docs/ROADMAP.md`.
`data/targets/required_documents.json` encodes what NY law requires each
district to publish — the yardstick for coverage gaps.
