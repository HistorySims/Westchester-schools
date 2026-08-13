"""Browser-based fetch for hosts that reject plain HTTP (the BoardDocs WAF).

BoardDocs 403s a datacenter httpx client even with browser headers and a primed
session. A real Chromium (Playwright — browsers are preinstalled at
``$PLAYWRIGHT_BROWSERS_PATH``) navigates the district's ``/Public`` page to clear
the challenge and set the session cookie, then downloads each ``$file`` URL
through the *same* browser context (``context.request``), so the cookies and
fingerprint carry over.

Optional by design: ``playwright`` is imported lazily inside ``start()``, so the
rest of the pipeline runs without it installed — the caller degrades to plain
HTTP (and keeps 403-ing on BoardDocs) rather than failing.
"""

from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)


class AsyncBrowserFetcher:
    """A reusable headless-Chromium fetcher. Prime a host once, then GET files."""

    def __init__(
        self,
        *,
        user_agent: str,
        nav_timeout_ms: int = 45_000,
        settle_ms: int = 1_500,
        request_timeout_ms: int = 45_000,
    ) -> None:
        self._user_agent = user_agent
        self._nav_timeout_ms = nav_timeout_ms
        self._settle_ms = settle_ms
        self._request_timeout_ms = request_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._primed: set[str] = set()

    async def start(self) -> None:
        """Launch Chromium. Raises if playwright isn't installed / can't launch."""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(user_agent=self._user_agent)

    async def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()

    async def prime(self, public_url: str) -> None:
        """Navigate a real page to ``public_url`` once, letting any JS challenge
        run and set the session cookie on the shared context."""
        if public_url in self._primed:
            return
        self._primed.add(public_url)  # mark first: a failed prime shouldn't retry every file
        page = await self._context.new_page()
        try:
            await page.goto(public_url, wait_until="domcontentloaded",
                            timeout=self._nav_timeout_ms)
            await page.wait_for_timeout(self._settle_ms)
        except Exception as exc:
            logger.warning("browser prime failed for %s: %s", public_url, exc)
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        """Download ``url`` through the browser context; raises on a non-OK status."""
        headers = {"Referer": referer} if referer else None
        resp = await self._context.request.get(
            url, headers=headers, timeout=self._request_timeout_ms
        )
        if not resp.ok:
            raise ValueError(f"browser fetch {resp.status} for {url}")
        return await resp.body()
