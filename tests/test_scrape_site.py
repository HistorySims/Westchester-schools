"""Tests for the district-website PDF crawler."""

from __future__ import annotations

import re

from herald.scrape.core import Fetcher
from herald.scrape.models import DocType
from herald.scrape.site import classify_link, crawl_site, extract_links


def _fast_fetcher() -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0, respect_robots=False)


def test_classify_link():
    assert classify_link("/f/Student-Handbook-2025.pdf", "Student Handbook") is DocType.handbook
    assert classify_link("/f/teacher-cba.pdf", "Teachers Collective Bargaining Agreement") \
        is DocType.contract
    assert classify_link("/f/PolicyManual.pdf", "Board Policy 5030") is DocType.policy
    assert classify_link("/f/adopted-budget-2025.pdf", "Adopted Budget") is DocType.budget
    assert classify_link("/f/minutes-3-17.pdf", "Meeting Minutes") is DocType.minutes
    assert classify_link("/f/lunch-menu.pdf", "March Lunch Menu") is None


def test_extract_links_resolves_and_filters():
    html = """
    <a href="/students/handbook.pdf">Handbook</a>
    <a href="https://cdn.x/y.pdf">CDN</a>
    <a href="#top">skip</a>
    <a href="mailto:a@b.c">skip</a>
    <a href="page2">Page 2</a>
    """
    links = extract_links(html, base_url="https://d.test/about/")
    urls = {u for u, _ in links}
    assert "https://d.test/students/handbook.pdf" in urls
    assert "https://cdn.x/y.pdf" in urls
    assert "https://d.test/about/page2" in urls   # relative resolved
    assert not any(u.startswith(("#", "mailto")) for u in urls)


def test_crawl_site_finds_targets_follows_same_domain_skips_offtarget(httpx_mock):
    httpx_mock.add_response(
        url="https://d.test/",
        headers={"Content-Type": "text/html"},
        text="""
        <a href="/students">Students &amp; Families</a>
        <a href="/files/Student-Handbook.pdf">Student Handbook</a>
        <a href="/files/newsletter.pdf">March Newsletter</a>
        <a href="https://cdn.other.com/Board-Policy-Manual.pdf">Policy Manual</a>
        """,
    )
    httpx_mock.add_response(
        url="https://d.test/students",
        headers={"Content-Type": "text/html"},
        text='<a href="/files/Teacher-Contract-CBA.pdf">Teachers Contract</a>',
    )

    with _fast_fetcher() as f:
        docs = list(crawl_site(f, base_url="https://d.test/", district="d"))

    by_type = {d.doc_type: d for d in docs}
    # handbook (home) + policy (cross-domain CDN pdf) + contract (followed page)
    assert DocType.handbook in by_type
    assert DocType.policy in by_type      # collected even though off-domain
    assert DocType.contract in by_type    # found by following the same-domain /students page
    # the newsletter PDF classified as nothing -> skipped under target_only
    assert all("newsletter" not in d.source_url for d in docs)
    assert by_type[DocType.handbook].suggested_filename == "Student-Handbook.pdf"


def test_crawl_site_respects_page_cap(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"https://d\.test/.*"),
        headers={"Content-Type": "text/html"},
        text='<a href="/board/next">board next</a>',
        is_reusable=True,
    )
    with _fast_fetcher() as f:
        list(crawl_site(f, base_url="https://d.test/", district="d", max_pages=5))
    # never fetches more than the cap even though every page links onward
    assert len(httpx_mock.get_requests()) <= 5
