"""Source-agnostic scraping plumbing: fetch, store, manifest.

Nothing here knows about BoardDocs or any specific site. Adapters use
``Fetcher`` to talk to the network; the ``runner`` uses ``RawStore`` and
``Manifest`` to persist what adapters discover.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from herald.scrape.models import DocType, ManifestEntry, ScrapedDoc

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Herald/0.1 (mailto:timhartnett29@gmail.com; "
    "+https://github.com/HistorySims/westchester-schools) "
    "school-district public-records research crawler"
)

# A current Chrome UA + the headers a real browser sends. Some hosts (BoardDocs
# among them) sit behind bot/WAF filters that 403 a non-browser client
# outright; presenting as a browser is the price of reaching public records
# there. We still identify a contact via the From header and keep the polite
# rate limiting, so this is "look like a browser", not "hide who we are".
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(text: str, *, maxlen: int = 120) -> str:
    """Filesystem-safe slug. Collapses runs of unsafe chars to ``-``."""
    text = _UNSAFE.sub("-", text).strip("-._")
    if not text:
        text = "untitled"
    return text[:maxlen]


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids fetching a URL for our User-Agent."""

    def __init__(self, url: str) -> None:
        super().__init__(f"blocked by robots.txt: {url}")
        self.url = url


class RobotsPolicy:
    """Per-host robots.txt cache answering can_fetch + crawl_delay.

    robots.txt is fetched through the same client (so it carries our
    User-Agent). Conventions: a 404 (no robots.txt) means allow-all; any
    other fetch failure is treated as allow-all but logged, so a flaky
    robots endpoint never silently blocks the whole crawl.
    """

    def __init__(self, fetch_text: object, user_agent: str) -> None:
        self._fetch_text = fetch_text  # callable(url) -> str | None
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}

    @staticmethod
    def _base(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _parser(self, url: str) -> RobotFileParser | None:
        base = self._base(url)
        if base not in self._cache:
            text = self._fetch_text(base + "/robots.txt")  # type: ignore[operator]
            if text is None:
                self._cache[base] = None  # allow-all
            else:
                rp = RobotFileParser()
                rp.parse(text.splitlines())
                rp.modified()  # mark as read so can_fetch evaluates the rules
                self._cache[base] = rp
        return self._cache[base]

    def can_fetch(self, url: str) -> bool:
        rp = self._parser(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        rp = self._parser(url)
        if rp is None:
            return None
        delay = rp.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None


class Fetcher:
    """A polite synchronous HTTP client.

    Manners, in order of how much they matter:
      * **serial** — one request at a time, never concurrent;
      * **robots.txt** — obeys Disallow and adopts any Crawl-delay (unless
        ``respect_robots=False``);
      * **rate limit** — at least ``min_request_interval`` seconds between
        requests, plus a little random jitter so we don't machine-gun at a
        fixed cadence;
      * **backpressure** — bounded retries with exponential backoff that
        honor ``Retry-After`` on 429/503;
      * **identity** — a User-Agent naming the project + a contact address.

    Kept sync because a crawler does not need concurrency, and sync is far
    easier to reason about (and to stop with Ctrl-C).
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        headers: dict[str, str] | None = None,
        min_request_interval: float = 2.0,
        jitter: float = 0.5,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
        timeout: float = 30.0,
        respect_robots: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.min_request_interval = min_request_interval
        self.jitter = jitter
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._last_request = 0.0
        self._owns_client = client is None
        base_headers = {"User-Agent": user_agent, **(headers or {})}
        self._client = client or httpx.Client(
            headers=base_headers,
            timeout=timeout,
            follow_redirects=True,
        )
        self.robots = RobotsPolicy(self._robots_text, user_agent) if respect_robots else None

    def _robots_text(self, url: str) -> str | None:
        """Low-level GET for robots.txt (bypasses throttle + robots check)."""
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("could not fetch %s (%s); assuming allow-all", url, exc)
            return None
        if resp.status_code == 200:
            return resp.text
        if resp.status_code != 404:
            logger.warning("robots.txt at %s -> HTTP %d; assuming allow-all", url, resp.status_code)
        return None

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _throttle(self, interval: float) -> None:
        if interval <= 0:
            return
        wait = interval - (time.monotonic() - self._last_request)
        wait = max(wait, 0.0) + random.uniform(0.0, self.jitter)
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
        """Issue a request with robots check + throttle + retry.

        Raises ``RobotsDisallowed`` if robots.txt forbids the URL, else the
        final transport/HTTP error on exhausted retries.
        """
        interval = self.min_request_interval
        if self.robots is not None:
            if not self.robots.can_fetch(url):
                raise RobotsDisallowed(url)
            crawl_delay = self.robots.crawl_delay(url)
            if crawl_delay is not None:
                interval = max(interval, crawl_delay)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(interval)
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
