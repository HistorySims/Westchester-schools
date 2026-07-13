# Scraping district sources

The `herald.scrape` package is the **acquisition** half of ingest
MILESTONE 1. It crawls a district source, downloads the raw artifacts
(PDF/HTML), and records each one in an append-only **manifest**. It does
*not* parse, chunk, embed, or write to the database — the ingest adapter
consumes the manifest this layer produces.

```
adapter (BoardDocs, …)  ─►  ScrapedDoc  ─►  Fetcher ─► RawStore (files)
                                                    └─► Manifest (jsonl)
                                                            │
                                            ingest adapter ◄┘  (later milestone)
```

## Layout

- `models.py` — `DocType`, `ScrapedDoc` (a discovered artifact),
  `ManifestEntry` (a downloaded one).
- `core.py` — source-agnostic plumbing: a polite `Fetcher` (identifying
  User-Agent, request spacing, bounded ret/backoff honoring `Retry-After`),
  a content-hashed `RawStore`, and a `Manifest` that doubles as the dedupe
  index so re-runs are cheap and resumable.
- `boarddocs.py` — first adapter. Pure parsers (`parse_meetings`,
  `parse_agenda_files`, …) isolated from the network client.
- `runner.py` — drives an adapter, downloads, dedupes, records.
- `__main__.py` — the `herald-scrape` CLI.

## Running (BoardDocs)

Most Westchester districts publish agendas, minutes, and policy manuals on
[BoardDocs](https://go.boarddocs.com) at
`https://go.boarddocs.com/<state>/<slug>/Board.nsf`. Find your district's
`<slug>` from that URL, then:

```bash
# 1. list the committees and grab the 'unique' id you want
herald-scrape committees --state ny --slug <slug>

# 2. eyeball a committee's meetings
herald-scrape meetings --state ny --slug <slug> --committee <id>

# 3. dry-run to see what WOULD download, then do it
herald-scrape fetch --state ny --slug <slug> --committee <id> \
    --district <slug> --since 2023-01-01 --dry-run
herald-scrape fetch --state ny --slug <slug> --committee <id> \
    --district <slug> --since 2023-01-01
```

Downloads land under `data/raw/<district>/<doc_type>/` and are indexed in
`data/raw/manifest.jsonl`.

> **Network note.** The build/CI environment is restricted to package
> registries, so the crawl must be run somewhere with open outbound network
> (your machine). The parsers and plumbing are fully unit-tested against
> fixtures regardless.

## Verify the BoardDocs contract on your first live run

The BoardDocs AJAX endpoints (`BD-GetMeetingsList`, `BD-GetAgenda`,
`BD-GetCommittees`) and their payload shapes are the documented public
contract, but they could not be exercised from the build environment. If a
district returns a different shape, the fix lives in the **pure parsers**
(`parse_committees` / `parse_meetings` / `parse_agenda_files`) — adjust
those, not the plumbing. Start with `herald-scrape committees …`; if it
prints your committees, the contract holds for that district.

## Adding another source

Write a new adapter module exposing an `iter_documents(...) -> Iterable[
ScrapedDoc]`, reuse `Fetcher` for network and `download_docs` for
persistence, and add a CLI command. Handbooks (loose district-site PDFs)
and meeting transcripts (YouTube/video portals) are the natural next two.
