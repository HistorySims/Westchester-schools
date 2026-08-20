"""Policy-manual portals: discovery + reconnaissance.

The adopted policy manual is the corpus's biggest hole. It is served by
neither of the two sources we already scrape:

* **BoardDocs** exposes a policy console (``BD-GetPolicyBooks`` /
  ``BD-GetPolicies`` / ``BD-GetPolicyItem``, recovered from ``policies.js``)
  but answers anonymous callers with ``No Access`` — every district in the
  peer set reports ``bd.policy_connected="NyssbaManagementConsole"``, i.e.
  the manual lives outside BoardDocs entirely.
* **District websites** link a handful of loose policy PDFs (Port Chester:
  ~18 discovered, 13 ingested) *plus* a link to the real manual — which the
  site crawler walks past, because it follows same-host pages and
  cross-domain PDFs, and a policy portal is a cross-domain HTML app.

Two vendors serve the peer set (see ``data/targets/policy_portals.json``):
``boardpolicyonline.com`` (legacy ``?b=<slug>`` → ``v3…/b/<slug>``) and
``policy.microscribepub.com`` (a Folio infobase behind ``om_isapi.dll``).

This module does the reconnaissance step only — find each district's portal
and capture what it actually returns — because both vendors are outside this
project's egress allowlist and can only be reached from a networked runner.
The parser is written from the captured output, not guessed, exactly as the
BoardDocs API was (docs/SCRAPING.md).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# A policy portal URL belonging to one of the known vendors.
PORTAL_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:boardpolicyonline\.com|microscribepub\.com)[^\s\"'<>]*",
    re.I,
)
# Pages worth following when hunting for the portal link.
_POLICY_PAGE = re.compile(r"polic", re.I)


def vendor_of(url: str) -> str | None:
    """Which portal vendor a URL belongs to, if any."""
    host = urlsplit(url).netloc.lower()
    if "boardpolicyonline" in host:
        return "boardpolicyonline"
    if "microscribepub" in host:
        return "microscribe"
    return None


def find_portal_links(html: str, *, base_url: str = "") -> list[str]:
    """Every policy-portal URL on a page (deduped, order preserved).

    Matches raw text as well as ``href``s: districts sometimes emit the portal
    link from JavaScript rather than an anchor.
    """
    seen: dict[str, None] = {}
    for m in PORTAL_RE.finditer(html or ""):
        seen.setdefault(m.group(0).replace("&amp;", "&"), None)
    if base_url:
        soup = BeautifulSoup(html or "", "html.parser")
        for a in soup.find_all("a", href=True):
            u = urljoin(base_url, a["href"]).replace("&amp;", "&")
            if vendor_of(u):
                seen.setdefault(u, None)
    return list(seen)


def policy_page_links(html: str, *, base_url: str) -> list[str]:
    """Same-host pages whose URL or link text mentions policy — discovery hops."""
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlsplit(base_url).netloc
    out: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        u = urljoin(base_url, a["href"]).split("#")[0]
        if urlsplit(u).netloc != host:
            continue
        if _POLICY_PAGE.search(u) or _POLICY_PAGE.search(a.get_text(" ", strip=True)):
            out.setdefault(u, None)
    return list(out)


def discover_portal(fetcher, start_url: str, *, max_pages: int = 6) -> list[str]:
    """Follow a district's policy page(s) until a portal URL turns up.

    Bounded BFS over same-host policy-ish pages; returns every portal URL
    found (a district may link both a root and per-section deep links).
    """
    queue = [start_url]
    seen: set[str] = set()
    found: dict[str, None] = {}
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = fetcher.get(url)
        except Exception as exc:
            logger.debug("policy discovery fetch failed %s: %s", url, exc)
            continue
        if "html" not in resp.headers.get("Content-Type", "").lower():
            continue
        for p in find_portal_links(resp.text, base_url=url):
            found.setdefault(p, None)
        if found:
            break
        queue.extend(u for u in policy_page_links(resp.text, base_url=url)
                     if u not in seen)
    return list(found)


@dataclass
class PortalProbe:
    """What one portal URL returned, for parser design."""

    district: str
    url: str
    vendor: str | None = None
    status: int | None = None
    final_url: str = ""
    content_type: str = ""
    bytes_len: int = 0
    title: str = ""
    frames: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    sample_links: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


def probe_portal(fetcher, district: str, url: str, *, save_to: Path | None = None
                 ) -> PortalProbe:
    """Fetch one portal and characterize it; optionally save the raw body.

    Captures the things that determine how to parse it: redirects (the legacy
    boardpolicyonline host bounces to a v3 path), framesets (the Folio viewer
    is frame-based), scripts (where the real endpoints hide, as with
    BoardDocs' policies.js), and forms (search-driven manuals).
    """
    p = PortalProbe(district=district, url=url, vendor=vendor_of(url))
    try:
        resp = fetcher.get(url)
    except Exception as exc:
        p.error = str(exc)[:300]
        return p
    p.status = resp.status_code
    p.final_url = str(resp.url)
    p.content_type = resp.headers.get("Content-Type", "")
    body = resp.text or ""
    p.bytes_len = len(body)
    if save_to is not None:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_text(body, encoding="utf-8")
    soup = BeautifulSoup(body, "html.parser")
    if soup.title and soup.title.string:
        p.title = soup.title.string.strip()[:200]
    p.frames = [urljoin(p.final_url, f.get("src", ""))
                for f in soup.find_all(["frame", "iframe"]) if f.get("src")][:10]
    p.scripts = [urljoin(p.final_url, s.get("src", ""))
                 for s in soup.find_all("script") if s.get("src")][:15]
    p.forms = [urljoin(p.final_url, f.get("action", "")) or p.final_url
               for f in soup.find_all("form")][:10]
    p.sample_links = [urljoin(p.final_url, a["href"])
                      for a in soup.find_all("a", href=True)][:25]
    return p


def load_portal_targets(path: str | Path) -> dict[str, dict]:
    """The districts block of ``data/targets/policy_portals.json``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["districts"]


def render_report(probes: list[PortalProbe], discovered: dict[str, list[str]]) -> str:
    """Markdown summary for the workflow run page."""
    lines = ["# Policy portal probe", ""]
    lines += ["| district | vendor | status | bytes | frames | scripts | title |",
              "|---|---|---:|---:|---:|---:|---|"]
    for p in probes:
        lines.append(
            f"| {p.district} | {p.vendor or '-'} | {p.status or p.error[:18] or '-'} "
            f"| {p.bytes_len} | {len(p.frames)} | {len(p.scripts)} | {p.title[:40]} |"
        )
    if discovered:
        lines += ["", "## Portals discovered from district pages", ""]
        for slug, urls in discovered.items():
            for u in urls:
                lines.append(f"- **{slug}** → `{u}`")
    lines += ["", "Raw bodies are in the artifact; the parser is written from "
              "these, not guessed."]
    return "\n".join(lines) + "\n"
