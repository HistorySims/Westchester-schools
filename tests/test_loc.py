"""Tests for the loc.gov-flavored LOC client (direct date enumeration)."""

from __future__ import annotations

import re
from datetime import date

import httpx
import pytest

from herald.loc import LOCBlocked, LOCClient, PageRef


def _client() -> LOCClient:
    # Disable the throttle and shorten retry delays so tests stay fast.
    return LOCClient(
        user_agent="test/1.0",
        min_request_interval=0.0,
        retry_base_delay=0.0,
        rate_limit_pad_secs=0.0,
    )


def _issue_url(d: str = "1842-04-22", ed: int = 1) -> str:
    return f"https://www.loc.gov/resource/sn83030213/{d}/ed-{ed}/?fo=json"


def _resources_response(seqs: list[int], d: str = "1842-04-22", ed: int = 1) -> dict:
    return {
        "item": {"date": d, "number_lccn": ["sn83030213"]},
        "resources": [
            {"url": f"https://www.loc.gov/resource/sn83030213/{d}/ed-{ed}/seq-{s}/"}
            for s in seqs
        ],
    }


# ---- public API contracts -------------------------------------------

@pytest.mark.asyncio
async def test_iter_issues_with_pages_requires_both_dates():
    async with _client() as loc:
        with pytest.raises(ValueError, match="date_from and date_to"):
            async for _ in loc.iter_issues_with_pages(
                "sn83030213", date_from=None, date_to=None,
            ):
                pass


# ---- single-date probing --------------------------------------------

@pytest.mark.asyncio
async def test_probe_single_date_with_resources_list(httpx_mock):
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        json=_resources_response([1, 2, 3, 4], "1845-08-09"),
    )
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            )
        ]
    assert len(out) == 1
    issue, pages = out[0]
    assert issue.date_issued == date(1845, 8, 9)
    assert issue.edition == 1
    assert [p.sequence for p in pages] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_404_on_ed_1_skips_date(httpx_mock):
    httpx_mock.add_response(url=_issue_url("1842-04-22", 1), status_code=404)
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1842, 4, 22), date_to=date(1842, 4, 22),
            )
        ]
    assert out == []


@pytest.mark.asyncio
async def test_pagination_total_fallback_when_no_resources_list(httpx_mock):
    """If the response has no `resources` array but `pagination.total` is
    set, we fab PageRefs for seq 1..total.
    """
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        json={"pagination": {"total": 6}, "item": {}},
    )
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            )
        ]
    assert len(out) == 1
    _issue, pages = out[0]
    assert [p.sequence for p in pages] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_last_resort_yields_seq_1_when_response_has_nothing(httpx_mock):
    """200 OK with neither resources nor pagination still means an issue
    exists — fall back to a single-page issue rather than skip the date.
    """
    httpx_mock.add_response(url=_issue_url("1845-08-09"), json={"item": {}})
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            )
        ]
    _issue, pages = out[0]
    assert [p.sequence for p in pages] == [1]


# ---- URL templates --------------------------------------------------

@pytest.mark.asyncio
async def test_page_ref_urls_use_legacy_chronam_pattern(httpx_mock):
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        json=_resources_response([1], "1845-08-09"),
    )
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            )
        ]
    _issue, pages = out[0]
    p = pages[0]
    assert p.image_url == (
        "https://chroniclingamerica.loc.gov/lccn/sn83030213/"
        "1845-08-09/ed-1/seq-1.jpg"
    )
    assert p.jp2_url is not None and p.jp2_url.endswith("seq-1.jp2")
    assert p.pdf_url is not None and p.pdf_url.endswith("seq-1.pdf")
    assert p.ocr_url == (
        "https://chroniclingamerica.loc.gov/lccn/sn83030213/"
        "1845-08-09/ed-1/seq-1/ocr.txt"
    )
    assert p.resource_url == (
        "https://www.loc.gov/resource/sn83030213/1845-08-09/ed-1/seq-1"
    )


# ---- multi-date enumeration -----------------------------------------

@pytest.mark.asyncio
async def test_walks_each_date_in_window(httpx_mock):
    # 3 dates: first has an issue, second is missing, third has an issue
    httpx_mock.add_response(
        url=_issue_url("1842-04-22"), json=_resources_response([1, 2], "1842-04-22"),
    )
    httpx_mock.add_response(url=_issue_url("1842-04-23"), status_code=404)
    httpx_mock.add_response(
        url=_issue_url("1842-04-24"), json=_resources_response([1], "1842-04-24"),
    )
    async with _client() as loc:
        out = [
            pair async for pair in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1842, 4, 22), date_to=date(1842, 4, 24),
            )
        ]
    assert [pair[0].date_issued for pair in out] == [
        date(1842, 4, 22), date(1842, 4, 24),
    ]
    assert [(p.sequence) for p in out[0][1]] == [1, 2]
    assert [(p.sequence) for p in out[1][1]] == [1]


# ---- error handling -------------------------------------------------

@pytest.mark.asyncio
async def test_500_at_resource_endpoint_retries_then_raises(httpx_mock):
    # 5xx is retried up to max_retries+1 times (default max_retries=3).
    for _ in range(4):
        httpx_mock.add_response(url=_issue_url("1845-08-09"), status_code=500)
    async with _client() as loc:
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            ):
                pass


@pytest.mark.asyncio
async def test_429_raises_blocked_without_retry(httpx_mock):
    """A 429 must hard-stop, not retry. LOC's block clears in ~1 hour;
    retrying during the window extends it."""
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        status_code=429,
        headers={"retry-after": "120"},
    )
    async with _client() as loc:
        with pytest.raises(LOCBlocked) as excinfo:
            async for _ in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            ):
                pass
    assert excinfo.value.retry_after_secs == 120


@pytest.mark.asyncio
async def test_html_interstitial_raises_blocked(httpx_mock):
    """An HTML body when JSON was requested means Cloudflare/CAPTCHA;
    same hard-stop behavior as 429."""
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html><body>Just a moment...</body></html>",
    )
    async with _client() as loc:
        with pytest.raises(LOCBlocked):
            async for _ in loc.iter_issues_with_pages(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 9),
            ):
                pass


# ---- fetch_ocr ------------------------------------------------------

def _page(lccn: str, d: date, seq: int) -> PageRef:
    legacy = (
        f"https://chroniclingamerica.loc.gov/lccn/{lccn}/"
        f"{d.isoformat()}/ed-1/seq-{seq}/ocr.txt"
    )
    return PageRef(
        lccn=lccn, date_issued=d, edition=1, sequence=seq,
        image_url="i.jpg", jp2_url="i.jp2", pdf_url="i.pdf",
        resource_url=f"https://www.loc.gov/resource/{lccn}/{d.isoformat()}/ed-1/seq-{seq}",
        ocr_url=legacy,
    )


_FULLTEXT_SERVICE_URL = (
    "https://tile.loc.gov/text-services/word-coordinates-service"
    "?segment=/service/ndnp/dlc/batch/data/sn83030213/.../0005.xml"
    "&format=alto_xml&full_text=1"
)

_ALTO_SAMPLE = """<?xml version="1.0"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">
  <Layout><Page><PrintSpace><TextBlock>
    <TextLine>
      <String CONTENT="ANTI-RENT" HPOS="100" VPOS="200" WIDTH="50" HEIGHT="20"/>
      <String CONTENT="EXCITEMENT." HPOS="160" VPOS="200" WIDTH="80" HEIGHT="20"/>
    </TextLine>
    <TextLine>
      <String CONTENT="From" HPOS="100" VPOS="230"/>
      <String CONTENT="our" HPOS="140" VPOS="230"/>
      <String CONTENT="correspondent." HPOS="170" VPOS="230"/>
    </TextLine>
  </TextBlock></PrintSpace></Page></Layout>
</alto>
"""


@pytest.mark.asyncio
async def test_fetch_ocr_follows_fulltext_service_and_parses_alto(httpx_mock):
    """Real LOC flow: resource JSON gives a fulltext_service URL pointing at
    ALTO XML; we follow it and extract <String CONTENT="..."> tokens."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn83030213/.*"),
        json={"fulltext_service": _FULLTEXT_SERVICE_URL, "item": {}},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://tile\.loc\.gov/text-services/.*"),
        text=_ALTO_SAMPLE,
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn83030213", date(1845, 8, 9), 1))
    assert "ANTI-RENT" in text
    assert "EXCITEMENT." in text
    assert "correspondent." in text


@pytest.mark.asyncio
async def test_fetch_ocr_unescapes_xml_entities_in_alto(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn83030213/.*"),
        json={"fulltext_service": _FULLTEXT_SERVICE_URL},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://tile\.loc\.gov/text-services/.*"),
        text='<String CONTENT="A&amp;B"/><String CONTENT="&quot;quoted&quot;"/>',
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn83030213", date(1845, 8, 9), 1))
    assert "A&B" in text
    assert '"quoted"' in text


@pytest.mark.asyncio
async def test_fetch_ocr_returns_empty_on_403_at_resource(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn83030213/.*"),
        status_code=403,
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn83030213", date(1845, 8, 9), 1))
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_ocr_returns_empty_on_404_at_resource(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn99999999/.*"),
        status_code=404,
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn99999999", date(1900, 1, 1), 1))
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_ocr_returns_empty_when_fulltext_service_missing(httpx_mock):
    """Some pages have a JSON resource but no fulltext_service link
    (genuinely no OCR available)."""
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn83030213/.*"),
        json={"item": {"title": "image-only page"}},
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn83030213", date(1845, 8, 9), 1))
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_ocr_returns_empty_when_fulltext_service_404s(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.loc\.gov/resource/sn83030213/.*"),
        json={"fulltext_service": _FULLTEXT_SERVICE_URL},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://tile\.loc\.gov/text-services/.*"),
        status_code=404,
    )
    async with _client() as loc:
        text = await loc.fetch_ocr(_page("sn83030213", date(1845, 8, 9), 1))
    assert text == ""


# ---- iter_issues backward compat shim -------------------------------

@pytest.mark.asyncio
async def test_iter_issues_yields_issue_refs_only(httpx_mock):
    httpx_mock.add_response(
        url=_issue_url("1845-08-09"),
        json=_resources_response([1, 2], "1845-08-09"),
    )
    httpx_mock.add_response(
        url=_issue_url("1845-08-10"),
        json=_resources_response([1], "1845-08-10"),
    )
    async with _client() as loc:
        issues = [
            i async for i in loc.iter_issues(
                "sn83030213",
                date_from=date(1845, 8, 9), date_to=date(1845, 8, 10),
            )
        ]
    assert [(i.date_issued, i.edition) for i in issues] == [
        (date(1845, 8, 9), 1),
        (date(1845, 8, 10), 1),
    ]
