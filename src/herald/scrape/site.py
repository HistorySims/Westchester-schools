"""District-website crawler: find PDF documents (handbooks, contracts, …).

Unlike the BoardDocs adapter this needs no per-site API — it just walks a
district site (bounded BFS, same domain) and collects links to PDFs, then
classifies each from its URL + anchor text. Downloads reuse the shared
runner/store/manifest. PDFs may live on a CDN, so PDF links are collected
cross-domain even though page crawling stays on the district's own host.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from herald.scrape.core import Fetcher
from herald.scrape.models import DocType, ScrapedDoc

logger = logging.getLogger(__name__)

# URL/anchor keyword -> doc type, in priority order (first match wins).
_RULES: list[tuple[re.Pattern[str], DocType]] = [
    (re.compile(r"handbook", re.I), DocType.handbook),
    (re.compile(r"collective\s*bargain|negotiat|bargaining\s*unit|\bcba\b|\bmou\b|\bmoa\b"
                r"|memorandum of (agreement|understanding)|\bcontract\b|\bagreement\b", re.I),
     DocType.contract),
    (re.compile(r"\bpolic(y|ies)\b|regulation|by-?law", re.I), DocType.policy),
    (re.compile(r"budget|adopted\s+budget|financial\s+statement", re.I), DocType.budget),
    (re.compile(r"minutes", re.I), DocType.minutes),
    (re.compile(r"agenda", re.I), DocType.agenda),
]

# Only follow HTML links that plausibly lead to documents, to bound the crawl.
_FOLLOW = re.compile(
    r"board|polic(y|ies)|handbook|student|famil|parent|contract|negotiat|budget"
    r"|human.?resourc|employ|department|district|about|minutes|agenda|document|finance",
    re.I,
)
_PDF = re.compile(r"\.pdf(?:$|\?)", re.I)


def classify_link(url: str, text: str) -> DocType | None:
    """Classify a PDF link from its URL + anchor text; None if not a target."""
    hay = f"{text} {url}"
    for pat, dt in _RULES:
        if pat.search(hay):
            return dt
    return None


def extract_links(html: str, *, base_url: str) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) for every ``<a href>`` on the page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        out.append((url, a.get_text(" ", strip=True)))
    return out


def _filename(url: str) -> str:
    from urllib.parse import unquote

    return unquote(url.split("?")[0].rsplit("/", 1)[-1]) or "document.pdf"


def crawl_site(
    fetcher: Fetcher,
    *,
    base_url: str,
    district: str,
    max_pages: int = 80,
    max_depth: int = 3,
    target_only: bool = True,
) -> Iterator[ScrapedDoc]:
    """Walk a district site (bounded) and yield a ScrapedDoc per PDF found.

    ``target_only`` keeps only PDFs that classify as a document type we want
    (handbook/contract/policy/…), skipping stray PDFs (forms, newsletters).
    """
    host = urlsplit(base_url).netloc
    seen_pages: set[str] = set()
    seen_pdfs: set[str] = set()
    queue: list[tuple[str, int]] = [(base_url, 0)]

    while queue and len(seen_pages) < max_pages:
        url, depth = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            resp = fetcher.get(url)
        except Exception as exc:  # one dead page shouldn't kill the crawl
            logger.debug("page fetch failed %s: %s", url, exc)
            continue
        if "html" not in resp.headers.get("Content-Type", "").lower():
            continue

        for link_url, text in extract_links(resp.text, base_url=url):
            if _PDF.search(link_url.split("?")[0]):
                if link_url in seen_pdfs:
                    continue
                seen_pdfs.add(link_url)
                dt = classify_link(link_url, text)
                if target_only and dt is None:
                    continue
                fname = _filename(link_url)
                yield ScrapedDoc(
                    district=district,
                    doc_type=dt or DocType.other,
                    title=text or fname,
                    source_url=link_url,
                    suggested_filename=fname,
                )
            elif (
                depth < max_depth
                and urlsplit(link_url).netloc == host
                and link_url not in seen_pages
                and _FOLLOW.search(link_url + " " + text)
            ):
                queue.append((link_url, depth + 1))
