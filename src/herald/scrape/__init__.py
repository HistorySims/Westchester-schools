"""Document acquisition layer for the Herald engine.

This subpackage is the *scraper* half of ingest MILESTONE 1: it crawls
district sources (BoardDocs, district websites, …), downloads the raw
artifacts (PDF/HTML), and records them in an append-only manifest. It does
**not** parse, chunk, embed, or touch the database — that is the job of the
ingest adapter, which reads the manifest this layer produces.

Design:
  * ``core`` — source-agnostic plumbing: a polite HTTP ``Fetcher``, a
    content-hashed ``RawStore``, and a JSONL ``Manifest`` with dedupe.
  * ``models`` — ``DocType`` / ``ScrapedDoc`` / ``ManifestEntry``.
  * per-source adapters (``boarddocs`` first) that discover ``ScrapedDoc``s.
  * ``runner`` — wires an adapter to the store + manifest.

Run it: ``python -m herald.scrape --help`` (see ``__main__``).
"""

from herald.scrape.models import DocType, ManifestEntry, ScrapedDoc

__all__ = ["DocType", "ManifestEntry", "ScrapedDoc"]
