"""Chronicling America client (loc.gov API).

Starting in 2025 the Chronicling America collection is served via the
loc.gov JSON API. The old ``chroniclingamerica.loc.gov/lccn/{lccn}.json``
endpoint now redirects to a malformed ``www.loc.gov`` URL and 403s, so
this client targets the new endpoints directly:

* Enumerate pages in a date window
      ``https://www.loc.gov/collections/chronicling-america/
        ?fa=number_lccn:{lccn}&dl=page
        &start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        &fo=json&c=200&sp={page}``

* Per-page detail incl. OCR ``full_text``
      ``{result.id}?fo=json``

The search results carry enough metadata for ``papers`` / ``issues`` /
``pages`` rows; only the OCR text requires the per-page resource fetch.

Rate-limit note: the Newspapers endpoint allows ~20 requests per 10s.
We add a small ``min_request_interval`` to stay safely under that.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

BASE = "https://www.loc.gov"
LEGACY_BASE = "https://chroniclingamerica.loc.gov"
COLLECTION_PATH = "/collections/chronicling-america/"
# LOC's Newspapers endpoint advertises two limits: 20 requests per 10
# seconds (burst) and 20 requests per 1 minute (sustained). In practice
# they appear to enforce more aggressively than that for sustained
# scraping — 3s/req still got us 429-banned after a few minutes.
# 6s/req puts us at 10 req/min, half the documented sustained ceiling.
DEFAULT_PER_PAGE = 25
DEFAULT_MIN_INTERVAL_SECS = 6.0
# Retry budget for transient errors. Previous 8/30 combination meant a
# single 429'd request could burn 5-8 minutes of backoff, and a chain
# of them ate ~40 min before failing. 3/10 fails fast: a 429-storm
# costs <= 5 min total for a 9-day window instead of an hour.
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 2.0
DEFAULT_RATE_LIMIT_PAD_SECS = 10.0
# Hard cap on paginated search depth. Vestigial — kept on the
# constructor so callers can still pass it for the (now-unused)
# search path; the active date-walking enumerator doesn't paginate.
DEFAULT_MAX_PAGINATION_DEPTH = 100


@dataclass(frozen=True)
class PaperMetadata:
    lccn: str
    title: str
    place: str | None
    start_year: int | None
    end_year: int | None


class LOCBlocked(RuntimeError):
    """LOC returned a rate-limit (429) or a Cloudflare/CAPTCHA HTML page.

    Per LOC's docs, a block from exceeding their rate clears in 1 hour;
    repeated retries during the block extend it. Raising this immediately
    lets the orchestrator stop the run instead of hammering.
    """

    def __init__(self, message: str, *, retry_after_secs: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_secs = retry_after_secs


@dataclass(frozen=True)
class IssueRef:
    lccn: str
    date_issued: date
    edition: int
    url: str  # canonical page-resource URL prefix on www.loc.gov


@dataclass(frozen=True)
class PageRef:
    lccn: str
    date_issued: date
    edition: int
    sequence: int
    image_url: str         # JPEG-derivative for the UI
    jp2_url: str | None    # high-res master, when LOC publishes one
    pdf_url: str | None
    resource_url: str      # loc.gov page resource URL (no .json suffix)
    ocr_url: str           # legacy chroniclingamerica.loc.gov ocr.txt URL


class LOCClient:
    """Async client for the loc.gov Chronicling America endpoints."""

    def __init__(
        self,
        *,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE,
        legacy_base_url: str = LEGACY_BASE,
        per_page: int = DEFAULT_PER_PAGE,
        min_request_interval: float = DEFAULT_MIN_INTERVAL_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        rate_limit_pad_secs: float = DEFAULT_RATE_LIMIT_PAD_SECS,
        max_pagination_depth: int = DEFAULT_MAX_PAGINATION_DEPTH,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._legacy = legacy_base_url.rstrip("/")
        self._per_page = per_page
        self._min_interval = min_request_interval
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._rate_limit_pad = rate_limit_pad_secs
        self._max_pagination_depth = max_pagination_depth
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
        self._last_request_ts = 0.0

    async def __aenter__(self) -> LOCClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- public API used by the orchestrator -------------------------

    async def get_paper_metadata(self, lccn: str) -> PaperMetadata:
        """Derive paper metadata from the first search hit for the LCCN.

        Avoids relying on a dedicated paper-level item endpoint, which
        the loc.gov migration is still shaking out. The fields we need
        (``partof_title``, ``location_*``) are present on every page hit.
        """
        params = self._search_params(
            lccn=lccn, date_from=None, date_to=None, page=1, per_page=1,
        )
        data = await self._get_json(COLLECTION_PATH, params=params)
        results = data.get("results", []) or []
        if not results:
            return PaperMetadata(lccn=lccn, title=lccn, place=None,
                                 start_year=None, end_year=None)
        r = results[0]
        title = _first(_as_list(r.get("partof_title"))) or lccn
        title = re.sub(r"\s*\[.*?\]\s*$", "", title).strip().rstrip(".") or lccn
        place = _format_place(r)
        return PaperMetadata(
            lccn=lccn, title=title, place=place,
            start_year=_to_int(r.get("dates")[0] if r.get("dates") else None),
            end_year=None,
        )

    async def iter_issues(
        self,
        lccn: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> AsyncIterator[IssueRef]:
        """Yield issues for the LCCN by walking each date in the window."""
        async for issue, _pages in self.iter_issues_with_pages(
            lccn, date_from=date_from, date_to=date_to,
        ):
            yield issue

    async def iter_issues_with_pages(
        self,
        lccn: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> AsyncIterator[tuple[IssueRef, list[PageRef]]]:
        """Yield ``(issue, pages)`` pairs by walking dates in the window.

        Bypasses LOC's ``dl=page`` collection search, which has been
        observed silently returning zero in-window pages for valid
        historic windows (e.g. 1842-04 of sn83030213). Direct date
        enumeration is more predictable: one HTTP request per
        (date, edition) probe, 404 → skip, 200 → extract pages.

        Requires both ``date_from`` and ``date_to`` (the search-based
        version handled None; date-walking can't enumerate an open range
        cheaply, so callers must specify the window).
        """
        if date_from is None or date_to is None:
            raise ValueError(
                "iter_issues_with_pages requires both date_from and date_to "
                "for direct date enumeration"
            )
        async for pair in self._iter_via_dates(
            lccn, date_from=date_from, date_to=date_to,
        ):
            yield pair

    async def list_pages(self, issue: IssueRef) -> list[PageRef]:
        """Return pages for ``issue`` as previously enumerated.

        With direct date enumeration the pages come back with the issue,
        so this is a single-day re-probe at the resource endpoint.
        """
        pages_by_issue = await self._pages_for_single_issue(issue)
        return pages_by_issue

    async def fetch_ocr(self, page: PageRef) -> str:
        """Fetch raw OCR text for a page from the loc.gov resource endpoint.

        Flow:
        1. GET the resource endpoint with ``?fo=json``. The response has a
           top-level ``fulltext_service`` URL pointing at LOC's text
           service. (The documented ``full_text`` JSON field doesn't
           actually appear for newspaper pages — confirmed empirically.)
        2. GET ``fulltext_service``. Returns ALTO XML by default.
        3. Parse ``<String CONTENT="word">`` tokens out of the ALTO XML
           and join. Falls back to returning the response body unchanged
           if it doesn't look like XML.

        Returns an empty string when OCR is unavailable for any reason
        (404, 403, missing fulltext_service field, parse failure). The
        orchestrator handles empty-OCR pages — they get a row but no
        chunks.
        """
        url = page.resource_url.rstrip("/") + "/?fo=json"
        try:
            resp = await self._get_with_retry(url)
        except httpx.HTTPStatusError:
            return ""
        if not resp.is_success:
            return ""
        try:
            data = resp.json()
        except ValueError:
            return ""

        ft_service = data.get("fulltext_service") if isinstance(data, dict) else None
        if not ft_service or not isinstance(ft_service, str):
            return ""

        try:
            ft_resp = await self._get_with_retry(ft_service)
        except httpx.HTTPStatusError:
            return ""
        if not ft_resp.is_success:
            return ""
        return _parse_alto_text(ft_resp.text)

    # ---- internals ---------------------------------------------------

    async def _pages_for_single_issue(self, issue: IssueRef) -> list[PageRef]:
        """Re-enumerate pages for one issue via the direct date probe."""
        out: list[PageRef] = []
        async for iss, pages in self._iter_via_dates(
            issue.lccn,
            date_from=issue.date_issued, date_to=issue.date_issued,
        ):
            if iss.edition == issue.edition:
                out.extend(pages)
        out.sort(key=lambda p: p.sequence)
        return out

    async def _iter_via_dates(
        self,
        lccn: str,
        *,
        date_from: date,
        date_to: date,
    ) -> AsyncIterator[tuple[IssueRef, list[PageRef]]]:
        """Walk each date in the window; probe ed-1..3 at the resource endpoint.

        For each (date, edition) we GET
        ``/resource/{lccn}/{date}/ed-{ed}/?fo=json``.

        * 404 → no issue at this (date, edition). When ed-1 is missing we
          assume no issue that day and skip the date; higher editions
          present without ed-1 are vanishingly rare for 19th-century papers.
        * 200 → the response carries enough metadata (a ``resources`` list
          or a ``pagination.total``) to construct ``PageRef`` per sequence.
          When neither field is present we fall back to assuming a single
          page (seq-1) because the issue clearly exists.
        """
        days = (date_to - date_from).days + 1
        if days <= 0:
            return
        for n in range(days):
            d = date_from + timedelta(days=n)
            # Probe ed-1 only. Multi-edition days are rare in 19th-century
            # papers; speculatively probing ed-2/ed-3 for every date doubles
            # or triples LOC requests for no win. If we ever need them,
            # detect via the resource response's metadata instead.
            pages = await self._probe_issue(lccn, d, 1)
            if pages is None:
                continue
            issue = IssueRef(
                lccn=lccn, date_issued=d, edition=1,
                url=_issue_resource_url(self._base, lccn, d, 1),
            )
            yield issue, pages

    async def _probe_issue(
        self, lccn: str, d: date, ed: int,
    ) -> list[PageRef] | None:
        """Return PageRefs for (lccn, date, ed), or None on 404."""
        url = f"{self._base}/resource/{lccn}/{d.isoformat()}/ed-{ed}/?fo=json"
        try:
            data = await self._get_json(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return self._pages_from_issue_data(data, lccn=lccn, d=d, ed=ed)

    def _pages_from_issue_data(
        self, data: dict, *, lccn: str, d: date, ed: int,
    ) -> list[PageRef]:
        """Build PageRefs from an issue-resource JSON response.

        LOC's response shape varies; we try, in order:
        1. ``resources`` — a list whose entries carry a ``url`` ending in
           ``/seq-{N}/``. This is the per-page enumeration we want.
        2. ``pagination.total`` — total page count for the issue; we fab
           PageRefs for seq 1..total using known URL templates.
        3. Last resort: a single-page issue (seq-1).
        """
        seqs: set[int] = set()
        for r in _as_list(data.get("resources")):
            if not isinstance(r, dict):
                continue
            for key in ("url", "id"):
                u = r.get(key) or ""
                m = re.search(r"/seq-(\d+)/?$", u if isinstance(u, str) else "")
                if m:
                    seqs.add(int(m.group(1)))
                    break

        if not seqs:
            total = (data.get("pagination") or {}).get("total")
            n = _to_int(total) or 0
            if n > 0:
                seqs.update(range(1, n + 1))

        if not seqs:
            # Response was 200 but yielded no enumerable pages — treat as
            # single-page issue. Better to ingest seq-1 than skip the date.
            seqs = {1}

        return sorted(
            (self._page_ref_for_seq(lccn, d, ed, seq) for seq in seqs),
            key=lambda p: p.sequence,
        )

    def _page_ref_for_seq(
        self, lccn: str, d: date, ed: int, seq: int,
    ) -> PageRef:
        legacy_seq = f"{self._legacy}/lccn/{lccn}/{d.isoformat()}/ed-{ed}/seq-{seq}"
        return PageRef(
            lccn=lccn, date_issued=d, edition=ed, sequence=seq,
            image_url=f"{legacy_seq}.jpg",
            jp2_url=f"{legacy_seq}.jp2",
            pdf_url=f"{legacy_seq}.pdf",
            resource_url=f"{self._base}/resource/{lccn}/{d.isoformat()}/ed-{ed}/seq-{seq}",
            ocr_url=f"{legacy_seq}/ocr.txt",
        )


    def _search_params(
        self,
        *,
        lccn: str,
        date_from: date | None,
        date_to: date | None,
        page: int,
        per_page: int,
    ) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("fa", f"number_lccn:{lccn}"),
            ("dl", "page"),
            ("fo", "json"),
            ("c", str(per_page)),
            ("sp", str(page)),
        ]
        if date_from:
            params.append(("start_date", date_from.isoformat()))
        if date_to:
            params.append(("end_date", date_to.isoformat()))
        # LOC's docs describe both ``start_date``/``end_date`` and ``dates=YYYY/YYYY``
        # — the former has been observed to be ignored on the chronam collection
        # endpoint, so we add the year-range form too. Whichever the server
        # actually honors, we win; the client-side filter is the final word.
        if date_from and date_to:
            params.append(("dates", f"{date_from.year}/{date_to.year}"))
        return params

    async def _get_json(self, path: str, *, params=None) -> dict:
        resp = await self._get_with_retry(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _get_with_retry(self, path: str, *, params=None) -> httpx.Response:
        """GET with exponential backoff on transient transport errors.

        LOC's search endpoint occasionally closes the connection mid-body
        (RemoteProtocolError); we also retry read timeouts and 5xx.

        Two responses get a hard stop instead of a retry, both raising
        ``LOCBlocked``:

        * **429 Too Many Requests** — LOC's block clears in ~1 hour
          wall-clock per their docs, and retrying inside that window
          extends the block. The orchestrator should kill the run, not
          loop.
        * **HTML body when JSON was requested** — Cloudflare bot
          interstitials come back as 200 with ``text/html``. Same hard
          stop: the body has no useful data and continuing to request
          just deepens whatever fingerprint Cloudflare matched on.

        4xx other than 429 is non-retryable and returned to the caller.
        """
        url = path if path.startswith("http") else f"{self._base}{path}"
        delay = self._retry_base_delay
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params)
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.ConnectError,
                httpx.ConnectTimeout,
            ) as e:
                last = e
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429:
                ra = resp.headers.get("retry-after")
                retry_after = int(ra) if ra and ra.isdigit() else None
                raise LOCBlocked(
                    f"LOC returned 429 for {url}; cool-off ~1 hour. "
                    f"Stopping rather than retrying — repeated requests "
                    f"during the block extend it.",
                    retry_after_secs=retry_after,
                )
            if _looks_like_html_interstitial(resp):
                raise LOCBlocked(
                    f"LOC returned an HTML body (likely Cloudflare bot "
                    f"challenge) for {url} when JSON was requested. "
                    f"Stopping rather than retrying."
                )
            if resp.status_code >= 500:
                last = httpx.HTTPStatusError(
                    f"loc {resp.status_code}", request=resp.request, response=resp,
                )
                if attempt >= self._max_retries:
                    raise last
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return resp
        # Loop always returns or raises; unreachable.
        raise last or RuntimeError("loc retry loop terminated unexpectedly")

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = self._last_request_ts + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_ts = loop.time()


# ---- helpers --------------------------------------------------------


def _looks_like_html_interstitial(resp: httpx.Response) -> bool:
    """Detect Cloudflare/CAPTCHA HTML when we asked for JSON.

    LOC's JSON endpoints normally return ``application/json``. A bot
    challenge comes back with HTML — sometimes 200 OK, sometimes 403,
    sometimes 503. ``Content-Type`` starting with ``text/html`` is the
    cheap and reliable signal; we don't need to parse the body.

    Skips the check on responses we never asked JSON for (e.g.
    ``fulltext_service`` endpoints that return ALTO XML or plain text).
    Those have an ``application/xml``, ``application/alto+xml``, or
    ``text/plain`` content-type, not ``text/html``.
    """
    ct = (resp.headers.get("content-type") or "").lower()
    return ct.startswith("text/html")


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_date(v: object) -> date:
    if isinstance(v, list):
        v = v[0] if v else None
    s = str(v) if v is not None else ""
    m = _DATE_RE.match(s)
    if not m:
        # 8-digit format also appears (YYYYMMDD)
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        raise ValueError(f"unparseable date: {v!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _to_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _as_list(v: object) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _first(xs):
    return xs[0] if xs else None


def _format_place(r: dict) -> str | None:
    city = _first(_as_list(r.get("location_city")))
    state = _first(_as_list(r.get("location_state")))
    parts = [p for p in (city, state) if p]
    return ", ".join(parts) if parts else None


def _issue_resource_url(base: str, lccn: str, d: date, ed: int) -> str:
    return f"{base}/resource/{lccn}/{d.isoformat()}/ed-{ed}/"


# Catches ``<String ... CONTENT="word" .../>`` regardless of attribute
# ordering. ALTO XML uses these for every recognized token on the page.
_ALTO_STRING_RE = re.compile(
    r'<String\b[^>]*\bCONTENT="([^"]*)"',
    re.IGNORECASE,
)


def _parse_alto_text(body: str) -> str:
    """Extract plain text from an ALTO XML document.

    ALTO encodes each recognized word as ``<String CONTENT="..."/>``
    inside nested ``<TextLine>`` / ``<TextBlock>`` elements. We grab the
    CONTENT attributes in document order and join with spaces — that's
    enough for a chunker; layout structure is irrelevant downstream.

    Defensive: returns the body unchanged when it doesn't look like
    ALTO XML, so a server that occasionally returns plain text still
    works.
    """
    if not body:
        return ""
    matches = _ALTO_STRING_RE.findall(body)
    if not matches:
        # Doesn't look like ALTO — maybe plain text already.
        return body
    # Crude unescape of the four XML entities that show up in OCR
    # CONTENT attributes. Stays well under stdlib's xml machinery cost.
    out_parts: list[str] = []
    for token in matches:
        token = (token
                 .replace("&amp;", "&")
                 .replace("&lt;", "<")
                 .replace("&gt;", ">")
                 .replace("&quot;", '"')
                 .replace("&apos;", "'"))
        out_parts.append(token)
    return " ".join(out_parts)
