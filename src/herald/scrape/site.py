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
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def parse_sitemap_locs(xml: str) -> list[str]:
    """All ``<loc>`` URLs in a sitemap or sitemap-index."""
    return _LOC.findall(xml or "")


def sitemap_urls(fetcher: Fetcher, root: str, *, max_maps: int = 20) -> list[str]:
    """Collect page + PDF URLs from /sitemap.xml (expanding sitemap indexes).

    Most school CMSs (Finalsite, Apptegy, …) render nav via JS, so a plain
    link crawl misses everything; the sitemap lists every real URL. Best-effort.
    """
    urls: list[str] = []
    queue = [f"{root}/sitemap.xml"]
    seen: set[str] = set()
    while queue and len(seen) < max_maps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            text = fetcher.get(sm).text
        except Exception as exc:  # no sitemap here is fine
            logger.debug("sitemap fetch failed %s: %s", sm, exc)
            continue
        for loc in parse_sitemap_locs(text):
            if loc.lower().endswith(".xml"):
                queue.append(loc)          # nested sitemap
            else:
                urls.append(loc)
    return urls


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


_GDOCS = re.compile(
    r"docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{15,})"
)
_GDRIVE = re.compile(
    r"drive\.google\.com/(?:file/d/|open\?id=|uc\?[^\"']*?id=)([A-Za-z0-9_-]{15,})"
)
_TITLE_JUNK = re.compile(r"^\W*\.?(pdf|docx?|xlsx?|pptx?)\b[\s:\u2013-]*", re.I)


def gdrive_download_url(url: str) -> str | None:
    """A direct-download/export URL for a Google Drive file or Google Doc.

    Many districts (Ossining, Port Chester …) store documents on Google Drive
    rather than as native PDFs. Files download via uc?export=download; Docs
    export to PDF. Returns None if the URL isn't a Drive/Docs link. (Drive
    *folders* aren't handled — they need enumeration.)
    """
    m = _GDOCS.search(url)
    if m:
        return f"https://docs.google.com/{m.group(1)}/d/{m.group(2)}/export?format=pdf"
    m = _GDRIVE.search(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def _as_document(url: str, text: str, district: str, *, target_only: bool) -> ScrapedDoc | None:
    """Build a ScrapedDoc if ``url`` is a document (native PDF or Drive/Doc)."""
    is_pdf = bool(_PDF.search(url.split("?")[0]))
    download = url if is_pdf else gdrive_download_url(url)
    if not download:
        return None
    dt = classify_link(url, text)
    if target_only and dt is None:
        return None
    title = _TITLE_JUNK.sub("", text).strip()
    fname = _filename(url) if is_pdf else (title or "document")
    return ScrapedDoc(
        district=district,
        doc_type=dt or DocType.other,
        title=title or fname,
        source_url=download,
        suggested_filename=fname,
    )


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
    parts = urlsplit(base_url)
    host = parts.netloc
    root = f"{parts.scheme}://{host}"
    seen_pages: set[str] = set()
    seen_docs: set[str] = set()
    queue: list[tuple[str, int]] = [(base_url, 0)]

    # Seed from the sitemap (reliable where JS-rendered nav hides links).
    for loc in sitemap_urls(fetcher, root):
        doc = _as_document(loc, "", district, target_only=target_only)
        if doc:
            if doc.source_url not in seen_docs:
                seen_docs.add(doc.source_url)
                yield doc
        elif urlsplit(loc).netloc == host and _FOLLOW.search(loc):
            queue.append((loc, 0))

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
            doc = _as_document(link_url, text, district, target_only=target_only)
            if doc:
                if doc.source_url not in seen_docs:
                    seen_docs.add(doc.source_url)
                    yield doc
            elif (
                depth < max_depth
                and urlsplit(link_url).netloc == host
                and link_url not in seen_pages
                and _FOLLOW.search(link_url + " " + text)
            ):
                queue.append((link_url, depth + 1))
