"""Source-agnostic scraping plumbing: fetch, store, manifest.

Nothing here knows about BoardDocs or any specific site. Adapters use
``Fetcher`` to talk to the network; the ``runner`` uses ``RawStore`` and
``Manifest`` to persist what adapters discover.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from herald.scrape.models import DocType, ManifestEntry, ScrapedDoc

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Herald/0.1 (mailto:timhartnett29@gmail.com; "
    "+https://github.com/HistorySims/westchester-schools) "
    "school-district public-records research crawler"
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(text: str, *, maxlen: int = 120) -> str:
    """Filesystem-safe slug. Collapses runs of unsafe chars to ``-``."""
    text = _UNSAFE.sub("-", text).strip("-._")
    if not text:
        text = "untitled"
    return text[:maxlen]


class Fetcher:
    """A polite synchronous HTTP client.

    Mirrors the manners of the old LOC client: a single identifying
    User-Agent, a minimum spacing between requests, and bounded retries with
    exponential backoff that honor ``Retry-After`` on 429/503. Kept sync
    because a locally-run crawler does not need the concurrency, and sync is
    far easier to reason about (and stop with Ctrl-C).
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval: float = 1.0,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.min_request_interval = min_request_interval
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._last_request = 0.0
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _throttle(self) -> None:
        if self.min_request_interval <= 0:
            return
        wait = self.min_request_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _sleep_for_retry(self, resp: httpx.Response | None, attempt: int) -> None:
        delay = self.retry_base_delay * (2**attempt)
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        time.sleep(delay)

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Issue a request with throttle + retry. Raises on final failure."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                self._last_request = time.monotonic()
            except httpx.TransportError as exc:  # network blip
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                logger.warning("network error on %s (attempt %d): %s", url, attempt + 1, exc)
                self._sleep_for_retry(None, attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                logger.warning("HTTP %d on %s, retrying", resp.status_code, url)
                self._sleep_for_retry(resp, attempt)
                continue
            resp.raise_for_status()
            return resp
        # Only reached if the loop exhausts on transport errors.
        assert last_exc is not None
        raise last_exc

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self.request("POST", url, **kwargs)


class RawStore:
    """Writes downloaded bytes under ``<base>/<district>/<doc_type>/``.

    Filenames are slugified and disambiguated with a short content-hash
    prefix so two documents that slug to the same name never collide.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def path_for(self, doc: ScrapedDoc, sha256: str, *, default_ext: str) -> Path:
        name = doc.suggested_filename or doc.title
        stem = slugify(Path(name).stem or doc.title)
        ext = Path(name).suffix.lower() or default_ext
        if not ext.startswith("."):
            ext = "." + ext
        fname = f"{sha256[:8]}_{stem}{ext}"
        return self.base_dir / slugify(doc.district) / doc.doc_type.value / fname

    def write(self, doc: ScrapedDoc, data: bytes, *, default_ext: str) -> Path:
        sha = sha256_bytes(data)
        path = self.path_for(doc, sha, default_ext=default_ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class Manifest:
    """Append-only JSONL record of every downloaded artifact.

    Doubles as the dedupe index: a document whose ``source_url`` (or content
    hash) is already present is skipped on re-runs, so crawls are resumable
    and cheap to repeat.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._seen_urls: set[str] = set()
        self._seen_hashes: set[str] = set()
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                self._seen_urls.add(rec["source_url"])
                self._seen_hashes.add(rec["sha256"])

    def has_url(self, url: str) -> bool:
        return url in self._seen_urls

    def has_hash(self, sha256: str) -> bool:
        return sha256 in self._seen_hashes

    def append(self, entry: ManifestEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")
        self._seen_urls.add(entry.source_url)
        self._seen_hashes.add(entry.sha256)

    def entries(self) -> list[ManifestEntry]:
        if not self.path.exists():
            return []
        out: list[ManifestEntry] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(ManifestEntry.model_validate_json(line))
        return out


def make_manifest_entry(
    doc: ScrapedDoc,
    *,
    local_path: Path,
    sha256: str,
    size_bytes: int,
    content_type: str | None,
) -> ManifestEntry:
    return ManifestEntry(
        district=doc.district,
        doc_type=doc.doc_type if isinstance(doc.doc_type, DocType) else DocType(doc.doc_type),
        title=doc.title,
        source_url=doc.source_url,
        local_path=str(local_path),
        sha256=sha256,
        size_bytes=size_bytes,
        content_type=content_type,
        date=doc.date,
        meeting_id=doc.meeting_id,
        committee=doc.committee,
        fetched_at=datetime.now(UTC),
    )
