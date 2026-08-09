"""Voyage AI embedding client.

Async, batched (128 inputs per request), with retry-on-429/5xx and bounded
exponential backoff. Returns plain ``list[float]`` per input so callers
don't depend on numpy.

See PLAN.md §7 for the embedding-model decision rationale.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

VOYAGE_API = "https://api.voyageai.com/v1/embeddings"

DEFAULT_MODEL = "voyage-3.5"
DEFAULT_BATCH = 128
EXPECTED_DIM = 1024
# Voyage caps a single request at 320k tokens across all inputs. Batching by
# count alone (128 inputs) overflows this when inputs are large — a whole-table
# chunk can be thousands of tokens — so we also cap a batch's estimated token
# sum. The estimate is chars/CHARS_PER_TOKEN with no tokenizer dependency, and
# it MUST over-count or a batch overflows: number-dense budget-table markdown
# (digits, commas, pipes) tokenizes at ~1.8 chars/token, far below prose's ~4,
# so 3.0 was too optimistic (a 280k-"estimated" batch was really ~470k and got
# rejected). 1.5 stays under the real count even for the densest tables, and a
# 250k budget then lands real requests around ~250k with comfortable margin.
MAX_BATCH_TOKENS = 250_000
CHARS_PER_TOKEN = 1.5
PER_INPUT_TOKEN_CAP = 32_000  # Voyage truncates each input to ~32k tokens


class VoyageError(RuntimeError):
    """Non-retryable error from the Voyage API."""


@dataclass(frozen=True)
class _Batch:
    indices: list[int]   # original positions in the caller's input list
    texts: list[str]


class VoyageEmbedder:
    """Thin async client for Voyage AI's embedding endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        batch_size: int = DEFAULT_BATCH,
        dim: int = EXPECTED_DIM,
        client: httpx.AsyncClient | None = None,
        base_url: str = VOYAGE_API,
        max_retries: int = 5,
        retry_base_delay: float = 1.0,
        max_batch_tokens: int = MAX_BATCH_TOKENS,
    ) -> None:
        if not api_key:
            raise ValueError("Voyage api_key is required")
        if batch_size <= 0 or batch_size > 128:
            raise ValueError("batch_size must be in 1..128")
        self._model = model
        self._batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens
        self._dim = dim
        self._base_url = base_url
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def __aenter__(self) -> VoyageEmbedder:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of corpus texts. Returns vectors in input order."""
        return await self._embed(texts, input_type="document")

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string."""
        out = await self._embed([text], input_type="query")
        return out[0]

    async def _embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        if not texts:
            return []
        # Drop empty texts but remember positions so we can stitch the output
        # back into the caller's index space with zero-vectors as placeholders.
        keep: list[_Batch] = []
        current = _Batch(indices=[], texts=[])
        cur_tokens = 0
        for i, t in enumerate(texts):
            if not t or not t.strip():
                continue
            est = min(int(len(t) / CHARS_PER_TOKEN) + 1, PER_INPUT_TOKEN_CAP)
            # Flush before adding if this input would push the batch past either
            # the count cap or the token budget (but never emit an empty batch).
            if current.texts and (
                len(current.texts) >= self._batch_size
                or cur_tokens + est > self._max_batch_tokens
            ):
                keep.append(current)
                current = _Batch(indices=[], texts=[])
                cur_tokens = 0
            current.indices.append(i)
            current.texts.append(t)
            cur_tokens += est
        if current.texts:
            keep.append(current)

        out: list[list[float]] = [[0.0] * self._dim] * len(texts)
        for batch in keep:
            vectors = await self._post_batch(batch.texts, input_type=input_type)
            for pos, vec in zip(batch.indices, vectors, strict=True):
                out[pos] = vec
        return out

    async def _post_batch(self, batch: list[str], *, input_type: str) -> list[list[float]]:
        payload = {"model": self._model, "input": batch, "input_type": input_type}
        delay = self._retry_base_delay
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(self._base_url, json=payload)
            except httpx.RequestError as e:
                last_err = e
                if attempt >= self._max_retries:
                    raise
                await self._sleep_with_jitter(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = VoyageError(f"voyage transient {resp.status_code}: {resp.text[:200]}")
                if attempt >= self._max_retries:
                    raise last_err
                # Honor Retry-After if present
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    await asyncio.sleep(int(ra))
                else:
                    await self._sleep_with_jitter(delay)
                    delay *= 2
                continue
            if not resp.is_success:
                raise VoyageError(f"voyage {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            items = sorted(data.get("data", []), key=lambda d: d["index"])
            vectors = [item["embedding"] for item in items]
            if len(vectors) != len(batch):
                raise VoyageError(
                    f"voyage returned {len(vectors)} vectors for {len(batch)} inputs"
                )
            for v in vectors:
                if len(v) != self._dim:
                    raise VoyageError(
                        f"voyage returned dim={len(v)} but client expected {self._dim}"
                    )
            return vectors

        # Defensive — loop always returns or raises inside.
        raise last_err or VoyageError("voyage: exhausted retries with no response")

    async def _sleep_with_jitter(self, delay: float) -> None:
        await asyncio.sleep(delay + random.random() * 0.5)
