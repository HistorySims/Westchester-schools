"""Voyage AI reranker client.

Reranks a candidate set of documents for a query using Voyage's
``rerank-2.5`` model. Same retry / backoff pattern as ``embed.py``.

See PLAN.md §8 for the place this slots into in the retrieval pipeline.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

VOYAGE_RERANK_API = "https://api.voyageai.com/v1/rerank"
DEFAULT_MODEL = "rerank-2.5"


class RerankError(RuntimeError):
    """Non-retryable error from the Voyage rerank API."""


@dataclass(frozen=True)
class RerankResult:
    """A single reranked document.

    ``index`` is the position in the caller's input list (preserved by
    Voyage), and ``relevance_score`` is the model's relevance estimate
    (higher = more relevant).
    """

    index: int
    relevance_score: float


class VoyageReranker:
    """Thin async client for Voyage's rerank endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        base_url: str = VOYAGE_RERANK_API,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("Voyage api_key is required")
        self._model = model
        self._base_url = base_url
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def __aenter__(self) -> VoyageReranker:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_k: int | None = None,
    ) -> list[RerankResult]:
        """Rerank ``documents`` against ``query``.

        Returns up to ``top_k`` results sorted by ``relevance_score``
        descending. When ``top_k`` is None, returns all input documents
        in reranked order. Empty ``documents`` returns ``[]`` without an
        HTTP call.
        """
        if not documents:
            return []
        payload: dict[str, object] = {
            "model": self._model,
            "query": query,
            "documents": documents,
        }
        if top_k is not None:
            payload["top_k"] = top_k

        delay = self._retry_base_delay
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(self._base_url, json=payload)
            except httpx.RequestError as e:
                last = e
                if attempt >= self._max_retries:
                    raise
                await self._sleep_with_jitter(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last = RerankError(
                    f"voyage rerank transient {resp.status_code}: {resp.text[:200]}"
                )
                if attempt >= self._max_retries:
                    raise last
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    await asyncio.sleep(int(ra))
                else:
                    await self._sleep_with_jitter(delay)
                    delay *= 2
                continue
            if not resp.is_success:
                raise RerankError(
                    f"voyage rerank {resp.status_code}: {resp.text[:500]}"
                )

            data = resp.json()
            items = data.get("data", []) or []
            results = [
                RerankResult(
                    index=int(item["index"]),
                    relevance_score=float(item["relevance_score"]),
                )
                for item in items
            ]
            # Voyage returns sorted desc by default, but be explicit.
            results.sort(key=lambda r: r.relevance_score, reverse=True)
            return results

        raise last or RerankError("voyage rerank: exhausted retries with no response")

    async def _sleep_with_jitter(self, delay: float) -> None:
        await asyncio.sleep(delay + random.random() * 0.5)
