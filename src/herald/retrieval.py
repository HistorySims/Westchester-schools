"""Hybrid retrieval: semantic (HNSW) + full-text (tsvector), RRF-merged,
then reranked with Voyage rerank-2.5.

See PLAN.md §8 for the design.

Flow:
  1. embed query with Voyage
  2. semantic_search -> top k_sem chunk ids by cosine distance
  3. fts_search    -> top k_fts chunk ids by ts_rank_cd
  4. Reciprocal Rank Fusion (k=60) merges the two ranked lists
  5. take top `rerank_top` for reranking
  6. fetch chunk metadata + content
  7. Voyage rerank-2.5 -> final ranking
  8. truncate to `final_top` and return
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

import psycopg

from herald import db
from herald.embed import VoyageEmbedder
from herald.rerank import VoyageReranker

DEFAULT_K_SEM = 50
DEFAULT_K_FTS = 50
DEFAULT_RRF_K = 60
DEFAULT_RERANK_TOP = 20
DEFAULT_FINAL_TOP = 12


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk worth showing the user, with everything needed for citation."""

    chunk_id: UUID
    content: str
    paper_lccn: str
    paper_title: str
    date_issued: date
    edition: int
    page_sequence: int
    image_url: str
    resource_url: str
    score: float          # rerank score if reranked, else RRF score
    rrf_score: float      # always set
    rerank_score: float | None  # None when rerank was skipped


class HybridRetriever:
    """Orchestrates the semantic + FTS + RRF + rerank pipeline."""

    def __init__(
        self,
        *,
        conn: psycopg.Connection,
        embedder: VoyageEmbedder,
        reranker: VoyageReranker | None = None,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._reranker = reranker

    async def retrieve(
        self,
        query: str,
        *,
        k_sem: int = DEFAULT_K_SEM,
        k_fts: int = DEFAULT_K_FTS,
        rrf_k: int = DEFAULT_RRF_K,
        rerank_top: int = DEFAULT_RERANK_TOP,
        final_top: int = DEFAULT_FINAL_TOP,
        paper_id: UUID | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        rerank: bool = True,
    ) -> list[RetrievedChunk]:
        """Run the full hybrid retrieval pipeline for ``query``."""
        if not query.strip():
            return []

        query_vec = await self._embedder.embed_query(query)

        with self._conn.cursor() as cur:
            sem_hits = db.semantic_search(
                cur,
                query_embedding=query_vec,
                k=k_sem,
                paper_id=paper_id,
                date_from=date_from,
                date_to=date_to,
            )
            fts_hits = db.fts_search(
                cur,
                query=query,
                k=k_fts,
                paper_id=paper_id,
                date_from=date_from,
                date_to=date_to,
            )

        sem_ids = [cid for cid, _ in sem_hits]
        fts_ids = [cid for cid, _ in fts_hits]
        rrf = reciprocal_rank_fusion([sem_ids, fts_ids], k=rrf_k)
        if not rrf:
            return []

        top_for_rerank_ids = [cid for cid, _ in rrf[:rerank_top]]

        with self._conn.cursor() as cur:
            details = db.fetch_chunk_details(cur, chunk_ids=top_for_rerank_ids)

        # Preserve RRF order while we have it.
        rrf_lookup = dict(rrf)
        ordered: list[tuple[UUID, float, db.ChunkHit]] = []
        for cid in top_for_rerank_ids:
            hit = details.get(cid)
            if hit is None:
                continue
            ordered.append((cid, rrf_lookup[cid], hit))

        if rerank and self._reranker is not None and ordered:
            # Voyage rerank takes the candidate documents in order; the
            # response gives us back indices into that list.
            docs = [hit.content for _, _, hit in ordered]
            rr = await self._reranker.rerank(query, docs, top_k=final_top)
            out: list[RetrievedChunk] = []
            for r in rr:
                cid, rrf_score, hit = ordered[r.index]
                out.append(_to_retrieved(
                    hit, rrf_score=rrf_score,
                    rerank_score=r.relevance_score,
                ))
            return out

        # No reranker: take the RRF order directly.
        return [
            _to_retrieved(hit, rrf_score=rrf_score, rerank_score=None)
            for _, rrf_score, hit in ordered[:final_top]
        ]


def reciprocal_rank_fusion(
    rankings: list[list[UUID]], *, k: int = DEFAULT_RRF_K,
) -> list[tuple[UUID, float]]:
    """Merge multiple ranked id lists via RRF.

    Each list is a leaderboard of doc ids (most relevant first). The
    RRF score for a doc is the sum across leaderboards of
    ``1 / (k + rank_i)`` where ``rank_i`` is 1-indexed position in
    leaderboard i (absent => no contribution from that leaderboard).

    Returns ``[(id, score), ...]`` sorted by score descending.
    """
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _to_retrieved(
    hit: db.ChunkHit,
    *,
    rrf_score: float,
    rerank_score: float | None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=hit.chunk_id,
        content=hit.content,
        paper_lccn=hit.paper_lccn,
        paper_title=hit.paper_title,
        date_issued=hit.date_issued,
        edition=hit.edition,
        page_sequence=hit.page_sequence,
        image_url=hit.image_url,
        resource_url=hit.resource_url,
        score=rerank_score if rerank_score is not None else rrf_score,
        rrf_score=rrf_score,
        rerank_score=rerank_score,
    )
