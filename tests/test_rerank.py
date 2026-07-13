"""Tests for the Voyage rerank-2.5 client."""

import pytest

from herald.rerank import RerankError, VoyageReranker


def _ok(items: list[tuple[int, float]]) -> dict:
    return {
        "object": "list",
        "data": [
            {"index": idx, "relevance_score": score, "document": "..."}
            for idx, score in items
        ],
        "model": "rerank-2.5",
        "usage": {"total_tokens": 42},
    }


@pytest.mark.asyncio
async def test_rerank_returns_results_sorted_desc(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(2, 0.91), (0, 0.55), (1, 0.20)]),
    )
    async with VoyageReranker(api_key="k") as rr:
        out = await rr.rerank("anti-rent", ["a", "b", "c"])
    assert [r.index for r in out] == [2, 0, 1]
    assert [round(r.relevance_score, 2) for r in out] == [0.91, 0.55, 0.20]


@pytest.mark.asyncio
async def test_rerank_empty_documents_short_circuits():
    async with VoyageReranker(api_key="k") as rr:
        out = await rr.rerank("q", [])
    assert out == []


@pytest.mark.asyncio
async def test_rerank_sends_top_k_when_provided(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(0, 0.5)]),
    )
    async with VoyageReranker(api_key="k") as rr:
        await rr.rerank("q", ["a", "b"], top_k=1)
    sent = httpx_mock.get_requests()[0].read()
    assert b'"top_k":1' in sent


@pytest.mark.asyncio
async def test_rerank_does_not_send_top_k_when_none(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(0, 0.5)]),
    )
    async with VoyageReranker(api_key="k") as rr:
        await rr.rerank("q", ["a"])
    sent = httpx_mock.get_requests()[0].read()
    assert b"top_k" not in sent


@pytest.mark.asyncio
async def test_rerank_retries_on_429(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        status_code=429, text="slow down",
    )
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(0, 0.5)]),
    )
    async with VoyageReranker(api_key="k", retry_base_delay=0.0) as rr:
        out = await rr.rerank("q", ["a"])
    assert len(out) == 1
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_rerank_retries_on_5xx(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        status_code=502, text="bad gateway",
    )
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(0, 0.5)]),
    )
    async with VoyageReranker(api_key="k", retry_base_delay=0.0) as rr:
        out = await rr.rerank("q", ["a"])
    assert len(out) == 1


@pytest.mark.asyncio
async def test_rerank_4xx_raises_immediately(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        status_code=400, text="bad request",
    )
    async with VoyageReranker(api_key="k", retry_base_delay=0.0) as rr:
        with pytest.raises(RerankError, match="400"):
            await rr.rerank("q", ["a"])
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_rerank_authorization_header(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/rerank",
        json=_ok([(0, 0.5)]),
    )
    async with VoyageReranker(api_key="my-secret") as rr:
        await rr.rerank("q", ["a"])
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("authorization") == "Bearer my-secret"


@pytest.mark.asyncio
async def test_constructor_requires_api_key():
    with pytest.raises(ValueError):
        VoyageReranker(api_key="")
