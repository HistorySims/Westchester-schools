"""Whole-manual extraction from BoardPolicyOnline (MicroScribe MSPI v3).

The adopted policy manual is the corpus's biggest hole, and it is the one
source that cannot be fetched. ``v3.boardpolicyonline.com`` is a **Blazor
Server** app: the page is a ~5 KB shell, every byte of policy text is rendered
server-side and pushed down a SignalR WebSocket, and there is no REST API to
call (``policy.js``/``shared.js``/``export.js`` contain no URLs at all). So a
plain HTTP client — which is all ``scrape/policy.py`` uses — can never see a
single policy.

What the app *does* expose is a bulk export. Its toolbar has a Print button
whose Blazor handler calls the JS interop function ``PrintDocument(html)``
(see ``/script/export.js``), passing **the whole selected subtree as one HTML
document**. Check the root node of the table-of-contents tree, click Print,
and the server hands the browser the entire manual — for Port Chester-Rye,
2.4 MB / 570 sections / 1.6 M characters, in one round trip.

So this module drives a real browser, overrides ``window.PrintDocument`` to
capture instead of print, and splits the result. The export is generously
structured: one ``div.export-section`` per policy, carrying ``id`` (the same
section id the portal's own deep links use, ``/b/<slug>/s/<id>``) and
``data-bookmark-title`` ("5100 ATTENDANCE"). That gives every policy a stable,
citable source URL — better provenance than the loose PDFs we scrape today.

Two practical notes, both learned the hard way:

* The app hard-depends on jQuery from ``code.jquery.com``. If that CDN is
  unreachable the Blazor circuit throws and the page renders only "An unknown
  error occurred while processing your request." We serve jQuery from
  ``assets/`` instead of the CDN so the scrape does not depend on a third
  party being up. Every other CDN asset (Bootstrap, PureCSS, CKEditor,
  jQuery UI, js-beautify) is cosmetic — the export works without them.
* Chromium's TLS 1.3 ClientHello is reset by some egress middleboxes while
  curl sails through. ``--ssl-version-max=tls1.2`` is offered via
  ``BrowserOptions.tls12`` for those environments; it is off by default.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: Vendored jQuery, served in place of the CDN copy the portal asks for.
JQUERY_ASSET = Path(__file__).parent / "assets" / "jquery-3.7.1.min.js"
#: Requests matching this are answered from :data:`JQUERY_ASSET`.
_JQUERY_URL_RE = re.compile(r"jquery-3[\d.]*\.min\.js", re.I)

#: Toolbar controls in the MSPI v3 UI.
TREEVIEW_SEL = "#published-policies-treeview"
ROOT_CHECKBOX_SEL = f"{TREEVIEW_SEL} input.k-checkbox"
PRINT_BTN_SEL = "#btnPrint"
PDF_BTN_SEL = "#btnPdf"

# Titles are "<number><suffix?> <NAME>". The suffix marks what kind of
# document shares the policy's number — "0110-R" is the regulation
# implementing policy 0110, "-E" an exhibit, "-Form" a form. They are separate
# documents in the manual but they answer questions about the same policy, so
# the number is captured without the suffix and the suffix kept beside it.
# A suffix must be *attached* to the number ("0110-R"), never merely the next
# word, or "0115 STUDENT HARASSMENT..." parses as suffix "STUDENT".
#   "5100 ATTENDANCE" / "0110.2-E ..." / "6170.R ..." / "5405-R(1) ..."
#   / "0340-E.1 ..." / "7331/7332/7333/7340 PLANS, SPECIFICATIONS ..."
_NUMBER_RE = re.compile(
    r"^\s*(\d{3,4}(?:\.\d+)*(?:/\d{3,4}(?:\.\d+)*)*)"   # 0110 | 0110.2 | 7331/7332
    r"(?:[-.]([A-Za-z][\w().-]*))?"                     # -R | -E.1 | .R | -R(1) | -E-1
    r"\s+(\S.*)$"
)
# A section whose body is shorter than this is a divider ("5000 STUDENT
# POLICIES"), not a policy. The shortest real policy in the peer set runs a
# few hundred characters; dividers have literally nothing under the title.
MIN_BODY_CHARS = 60


@dataclass
class PolicyDoc:
    """One policy, split out of the manual export."""

    section_id: str
    title: str
    number: str | None
    text: str
    html: str
    #: "R" (regulation), "E" (exhibit), "Form" — empty for the policy itself.
    suffix: str = ""
    source_url: str = ""

    @property
    def full_number(self) -> str | None:
        """``0110.2-E`` — the number as the manual writes it."""
        if not self.number:
            return None
        return f"{self.number}-{self.suffix}" if self.suffix else self.number

    @property
    def is_substantive(self) -> bool:
        return len(self.text) >= MIN_BODY_CHARS


@dataclass
class BrowserOptions:
    """Knobs for the headless browser, kept out of the scraping logic."""

    headless: bool = True
    #: Explicit Chromium binary (e.g. a preinstalled ``/opt`` browser).
    executable_path: str | None = None
    #: ``{"server": "http://127.0.0.1:8080"}`` — Playwright proxy config.
    proxy: dict | None = None
    #: Cap TLS at 1.2. Some egress middleboxes reset Chromium's TLS 1.3
    #: ClientHello (post-quantum key share) while other clients are fine.
    tls12: bool = False
    #: How long to wait for the server to render the export, in seconds.
    export_timeout: float = 180.0
    extra_args: list[str] = field(default_factory=list)

    def launch_args(self) -> list[str]:
        args = list(self.extra_args)
        if self.tls12:
            args.append("--ssl-version-max=tls1.2")
        return args


def portal_base(url: str) -> str:
    """``https://v3.boardpolicyonline.com/b/port_chester_rye`` from any deep link.

    The export's section ids are only meaningful against the book they came
    from, so provenance URLs are built from the *final* portal URL.
    """
    parts = urlsplit(url)
    seg = [p for p in parts.path.split("/") if p]
    if len(seg) >= 2 and seg[0] == "b":
        return f"{parts.scheme}://{parts.netloc}/b/{seg[1]}"
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _section_text(div) -> str:
    """Body text of one export section: title and boilerplate footer removed."""
    clone = BeautifulSoup(str(div), "html.parser")
    # NB: the section wrapper itself carries ``page-break``, so a bare
    # ``.page-break`` selector would decompose the whole policy.
    for junk in clone.select("p.section-title, div.section-footer"):
        junk.decompose()
    text = clone.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def split_export(html: str, *, portal_url: str = "") -> list[PolicyDoc]:
    """Every policy in a manual export, in manual order.

    Section dividers (a title with no body) are returned too but report
    ``is_substantive == False``; callers decide whether to keep them. Nothing
    is silently dropped — a policy that looks empty is a fact about the
    manual, and quietly discarding it is how a corpus grows blind spots.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    base = portal_base(portal_url) if portal_url else ""
    out: list[PolicyDoc] = []
    for div in soup.select("div.export-section"):
        title = (div.get("data-bookmark-title") or "").strip()
        if not title:
            node = div.select_one("p.section-title")
            title = node.get_text(" ", strip=True) if node else ""
        sid = (div.get("id") or "").strip()
        m = _NUMBER_RE.match(title)
        out.append(
            PolicyDoc(
                section_id=sid,
                title=title,
                number=m.group(1) if m else None,
                suffix=(m.group(2) or "") if m else "",
                text=_section_text(div),
                html=str(div),
                source_url=f"{base}/s/{sid}" if base and sid else portal_url,
            )
        )
    return out


async def _install_asset_routes(context) -> None:
    """Serve jQuery locally; let everything else go to the network."""
    jq = JQUERY_ASSET.read_text(encoding="utf-8")

    async def handler(route):
        if _JQUERY_URL_RE.search(route.request.url):
            await route.fulfill(
                status=200, content_type="application/javascript", body=jq
            )
        else:
            await route.continue_()

    await context.route("**/*", handler)


async def fetch_manual_export_async(
    url: str, *, options: BrowserOptions | None = None
) -> tuple[str, str]:
    """Drive the portal and return ``(export_html, final_url)``.

    Selects the whole manual in the table-of-contents tree, clicks Print, and
    captures the HTML the server hands to ``window.PrintDocument``.
    """
    from playwright.async_api import async_playwright  # optional dependency

    opts = options or BrowserOptions()
    captured: list[str] = []
    done = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=opts.headless,
            executable_path=opts.executable_path,
            proxy=opts.proxy,
            args=opts.launch_args(),
        )
        try:
            context = await browser.new_context()
            await _install_asset_routes(context)
            page = await context.new_page()

            async def grab(_source, html: str) -> None:
                captured.append(html or "")
                done.set()

            await page.expose_binding("__heraldGrab", grab)
            # The interop functions are defined by a deferred script, so poll
            # until they exist rather than racing the page load.
            await page.add_init_script(
                """
                const hook = () => {
                  if (typeof window.PrintDocument === 'function'
                      && !window.PrintDocument.__herald) {
                    window.PrintDocument = function (html) {
                      window.__heraldGrab(html || ''); return true;
                    };
                    window.PrintDocument.__herald = true;
                  }
                };
                setInterval(hook, 100);
                """
            )

            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_selector(TREEVIEW_SEL, timeout=90_000)
            final_url = page.url
            await page.wait_for_timeout(3_000)  # let the tree bind

            await page.locator(ROOT_CHECKBOX_SEL).first.check(force=True)
            await page.wait_for_timeout(3_000)  # selection propagates over SignalR
            await page.click(PRINT_BTN_SEL)
            logger.info("print requested for %s; waiting for server render", final_url)

            try:
                await asyncio.wait_for(done.wait(), timeout=opts.export_timeout)
            except TimeoutError:
                raise RuntimeError(
                    f"export timed out after {opts.export_timeout}s for {url}"
                ) from None
        finally:
            await browser.close()

    return captured[0], final_url


def fetch_manual_export(url: str, *, options: BrowserOptions | None = None) -> tuple[str, str]:
    """Blocking wrapper around :func:`fetch_manual_export_async`."""
    return asyncio.run(fetch_manual_export_async(url, options=options))
