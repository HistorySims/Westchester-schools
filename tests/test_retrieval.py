"""Tests for the hybrid retrieval orchestrator."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from herald.db import ChunkHit
from herald.rerank import RerankResult
from herald.retrieval import (
    HybridRetriever,
    RetrievedChunk,
    reciprocal_rank_fusion,
)

A = UUID(int=1)
B = UUID(int=2)
C = UUID(int=3)
D = UUID(int=4)


# ---- RRF unit tests --------------------------------------------------

def test_rrf_simple_two_lists():
    """Doc at the top of both lists should beat doc at top of one."""
    rrf = reciprocal_rank_fusion([[A, B, C], [B, A, D]], k=60)
    by_id = dict(rrf)
    # A: 1/(60+1) + 1/(60+2) = 0.01639 + 0.01613 = 0.03252
    # B: 1/(60+2) + 1/(60+1) = same = 0.03252  -> tie
    # C: 1/(60+3) = 0.01587
    # D: 1/(60+3) = 0.01587
    assert round(by_id[A], 5) == round(by_id[B], 5)
    assert by_id[A] > by_id[C]
    assert by_id[A] > by_id[D]


def test_rrf_doc_in_one_list_only():
    rrf = reciprocal_rank_fusion([[A, B], [C, D]], k=60)
    by_id = dict(rrf)
    # All four docs appear, all at rank 1 or 2 of one list only.
    assert len(rrf) == 4
    # A and C both at rank 1 in their lists -> equal score
    assert round(by_id[A], 5) == round(by_id[C], 5)
    # A > B (rank 1 vs rank 2 in same-length list)
    assert by_id[A] > by_id[B]


def test_rrf_empty_inputs():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_single_list():
    rrf = reciprocal_rank_fusion([[A, B, C]], k=60)
    assert [doc for doc, _ in rrf] == [A, B, C]


# ---- end-to-end retrieval, with fakes ---------------------------------

class FakeEmbedder:
    """Returns a deterministic query vector."""

    async def embed_query(self, text: str) -> list[float]:
        _ = text
        return [0.1] * 1024


class FakeReranker:
    """Reranks by reversing the input order — useful to see if reranking
    actually got applied vs. RRF order leaking through."""

    async def rerank(
        self, query: str, documents: list[str], *, top_k: int | None = None,
    ) -> list[RerankResult]:
        _ = query
        n = len(documents)
        cut = n if top_k is None else min(top_k, n)
        # Highest "relevance" for the LAST item, lowest for the FIRST.
        return [
            RerankResult(index=n - 1 - i, relevance_score=1.0 - i / max(n, 1))
            for i in range(cut)
        ]


class FakeCursor:
    def __init__(self, owner: FakeConn) -> None:
        self._owner = owner
        self._last_response: object = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        s = " ".join(sql.split()).lower()
        if "embedding <=> %(qvec)s::vector" in s:
            self._last_response = self._owner.semantic_results
        elif "websearch_to_tsquery" in s and "ts_rank_cd" in s:
            self._last_response = self._owner.fts_results
        elif "from chunks c join pages" in s and "where c.id = any" in s:
            self._last_response = self._owner.detail_rows
        else:
            self._last_response = []

    def fetchall(self):
        return self._last_response or []


class FakeConn:
    def __init__(
        self, *,
        semantic_results: list[tuple[UUID, float]],
        fts_results: list[tuple[UUID, float]],
        detail_rows: list[tuple],
    ) -> None:
        self.semantic_results = semantic_results
        self.fts_results = fts_results
        self.detail_rows = detail_rows

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _detail_row(cid: UUID, date_str: str = "1845-08-09", seq: int = 1) -> tuple:
    return (
        cid, f"content for {cid.int}", "sn83030213", "Tribune",
        date.fromisoformat(date_str), 1, seq,
        f"https://chroniclingamerica.loc.gov/lccn/sn83030213/{date_str}/ed-1/seq-{seq}.jpg",
        UUID(int=99),  # page_id — unused by ChunkHit
    )


@pytest.mark.asyncio
async def test_retrieve_happy_path_with_rerank():
    conn = FakeConn(
        semantic_results=[(A, 0.1), (B, 0.2), (C, 0.3)],
        fts_results=[(B, 1.0), (D, 0.5), (A, 0.2)],
        detail_rows=[_detail_row(A), _detail_row(B), _detail_row(C), _detail_row(D)],
    )
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
    )
    out = await r.retrieve("anti-rent", final_top=3)
    assert len(out) == 3
    # FakeReranker reverses → last RRF candidate ranks first
    assert isinstance(out[0], RetrievedChunk)
    assert all(h.rerank_score is not None for h in out)
    assert all(h.score == h.rerank_score for h in out)


@pytest.mark.asyncio
async def test_retrieve_without_reranker_uses_rrf_order():
    conn = FakeConn(
        semantic_results=[(A, 0.1), (B, 0.2)],
        fts_results=[(B, 1.0), (A, 0.5)],
        detail_rows=[_detail_row(A), _detail_row(B)],
    )
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        reranker=None,
    )
    out = await r.retrieve("anti-rent", final_top=5)
    assert len(out) == 2
    assert all(h.rerank_score is None for h in out)
    assert all(h.score == h.rrf_score for h in out)


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty():
    conn = FakeConn(semantic_results=[], fts_results=[], detail_rows=[])
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
    )
    out = await r.retrieve("")
    assert out == []
    out = await r.retrieve("   ")
    assert out == []


@pytest.mark.asyncio
async def test_retrieve_no_hits_returns_empty():
    conn = FakeConn(semantic_results=[], fts_results=[], detail_rows=[])
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
    )
    out = await r.retrieve("nothing matches")
    assert out == []


@pytest.mark.asyncio
async def test_retrieve_skips_chunks_missing_from_details():
    """If a chunk id surfaces in retrieval but has no detail row (e.g. deleted
    between queries), it's quietly dropped instead of crashing."""
    conn = FakeConn(
        semantic_results=[(A, 0.1), (B, 0.2)],
        fts_results=[],
        detail_rows=[_detail_row(A)],  # B missing
    )
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        reranker=None,
    )
    out = await r.retrieve("query")
    assert len(out) == 1
    assert out[0].chunk_id == A


@pytest.mark.asyncio
async def test_retrieve_truncates_to_final_top_when_no_rerank():
    conn = FakeConn(
        semantic_results=[(A, 0.1), (B, 0.2), (C, 0.3), (D, 0.4)],
        fts_results=[],
        detail_rows=[_detail_row(A), _detail_row(B), _detail_row(C), _detail_row(D)],
    )
    r = HybridRetriever(
        conn=conn,  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        reranker=None,
    )
    out = await r.retrieve("q", final_top=2)
    assert len(out) == 2
    # RRF order with one list: top-2 are first two of semantic_results
    assert [h.chunk_id for h in out] == [A, B]


def test_chunkhit_dataclass_is_what_fetch_returns():
    """Smoke check that the dataclass fields the orchestrator uses
    actually live on ChunkHit, so refactors don't silently drift."""
    h = ChunkHit(
        chunk_id=A, content="x", paper_lccn="sn1", paper_title="P",
        date_issued=date(1845, 1, 1), edition=1, page_sequence=1,
        image_url="i", resource_url="r",
    )
    assert h.chunk_id == A and h.score == 0.0
