import pytest

from herald.embed import VoyageEmbedder, VoyageError


def _ok_response(n: int, *, dim: int = 1024) -> dict:
    # Use (i+1) so no real vector accidentally equals the zero placeholder.
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(i + 1)] * dim}
            for i in range(n)
        ],
        "model": "voyage-3.5",
        "usage": {"total_tokens": 12},
    }


@pytest.mark.asyncio
async def test_embed_documents_single_batch(httpx_mock):
    httpx_mock.add_response(
        url="https://api.voyageai.com/v1/embeddings",
        json=_ok_response(3),
    )
    async with VoyageEmbedder(api_key="k") as ve:
        out = await ve.embed_documents(["a", "b", "c"])
    assert len(out) == 3
    assert all(len(v) == 1024 for v in out)
    assert out[0][0] == 1.0
    assert out[1][0] == 2.0
    assert out[2][0] == 3.0


@pytest.mark.asyncio
async def test_embed_documents_splits_across_batches(httpx_mock):
    # Embed 5 with batch_size=2 -> 3 API calls (2+2+1)
    httpx_mock.add_response(json=_ok_response(2))
    httpx_mock.add_response(json=_ok_response(2))
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="k", batch_size=2) as ve:
        out = await ve.embed_documents(["a", "b", "c", "d", "e"])
    assert len(out) == 5
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_embed_documents_empty_input_no_http():
    async with VoyageEmbedder(api_key="k") as ve:
        out = await ve.embed_documents([])
    assert out == []


@pytest.mark.asyncio
async def test_embed_documents_filters_blank_strings(httpx_mock):
    # Two blanks among five inputs -> only 3 sent
    httpx_mock.add_response(json=_ok_response(3))
    async with VoyageEmbedder(api_key="k") as ve:
        out = await ve.embed_documents(["a", "", "b", "   ", "c"])
    assert len(out) == 5
    # blanks become zero-vector placeholders
    assert out[1] == [0.0] * 1024
    assert out[3] == [0.0] * 1024
    # non-blanks have non-placeholder vectors
    assert out[0] != [0.0] * 1024
    assert out[2] != [0.0] * 1024
    assert out[4] != [0.0] * 1024


@pytest.mark.asyncio
async def test_embed_query_returns_single_vector(httpx_mock):
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="k") as ve:
        v = await ve.embed_query("when did the Calico Indians form")
    assert len(v) == 1024
    sent = httpx_mock.get_requests()[0].read()
    assert b'"input_type":"query"' in sent


@pytest.mark.asyncio
async def test_embed_documents_sends_document_input_type(httpx_mock):
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="k") as ve:
        await ve.embed_documents(["doc"])
    sent = httpx_mock.get_requests()[0].read()
    assert b'"input_type":"document"' in sent


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(httpx_mock):
    httpx_mock.add_response(status_code=429, text="slow down")
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="k", retry_base_delay=0.0) as ve:
        out = await ve.embed_documents(["a"])
    assert len(out) == 1
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_retries_on_500_then_succeeds(httpx_mock):
    httpx_mock.add_response(status_code=503, text="overloaded")
    httpx_mock.add_response(status_code=502, text="bad gateway")
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="k", retry_base_delay=0.0) as ve:
        out = await ve.embed_documents(["a"])
    assert len(out) == 1


@pytest.mark.asyncio
async def test_4xx_raises_immediately(httpx_mock):
    httpx_mock.add_response(status_code=400, text="bad request")
    async with VoyageEmbedder(api_key="k", retry_base_delay=0.0) as ve:
        with pytest.raises(VoyageError, match="400"):
            await ve.embed_documents(["a"])
    assert len(httpx_mock.get_requests()) == 1  # no retry


@pytest.mark.asyncio
async def test_dim_mismatch_raises(httpx_mock):
    httpx_mock.add_response(json=_ok_response(1, dim=512))
    async with VoyageEmbedder(api_key="k") as ve:
        with pytest.raises(VoyageError, match="dim"):
            await ve.embed_documents(["a"])


@pytest.mark.asyncio
async def test_count_mismatch_raises(httpx_mock):
    # Server returns 2 vectors for 3 inputs.
    httpx_mock.add_response(json=_ok_response(2))
    async with VoyageEmbedder(api_key="k") as ve:
        with pytest.raises(VoyageError, match="2 vectors"):
            await ve.embed_documents(["a", "b", "c"])


@pytest.mark.asyncio
async def test_constructor_validates_inputs():
    with pytest.raises(ValueError):
        VoyageEmbedder(api_key="")
    with pytest.raises(ValueError):
        VoyageEmbedder(api_key="k", batch_size=0)
    with pytest.raises(ValueError):
        VoyageEmbedder(api_key="k", batch_size=129)


@pytest.mark.asyncio
async def test_authorization_header_sent(httpx_mock):
    httpx_mock.add_response(json=_ok_response(1))
    async with VoyageEmbedder(api_key="my-secret-key") as ve:
        await ve.embed_documents(["a"])
    req = httpx_mock.get_requests()[0]
    assert req.headers.get("authorization") == "Bearer my-secret-key"
