"""Panel retrieval over the schools corpus: per-district evidence, fused.

The retrieval primitive here is deliberately *not* global top-k. The
questions this corpus exists to answer ("what's the normal cell-phone
policy?", "which districts are doing Middle States accreditation?",
"who pays coaches unusually much?") are **panel questions**: the district
is the unit of analysis, and an answer needs the best evidence *from each
district* — including the honest observation that a district produced
none. Global top-k lets one verbose district crowd out the rest and can
never report absence. So both search legs rank **per district** (SQL
window functions), RRF fuses them per district, and an optional Voyage
rerank sharpens each district's final picks.

At the current corpus size (~25k chunks) the window-function scan is
exact — no ANN index shortcuts — which is both simpler and more accurate.
Revisit if the corpus grows past a few hundred thousand chunks.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

import psycopg

from herald.embed import VoyageEmbedder
from herald.rerank import VoyageReranker

RRF_K = 60                  # standard reciprocal-rank-fusion constant
DEFAULT_POOL = 12           # candidates per district per leg, pre-fusion
DEFAULT_PER_DISTRICT = 4    # evidence chunks per district after fusion/rerank


@dataclass
class EvidenceChunk:
    """One retrieved chunk, with everything a citation needs."""

    chunk_id: UUID
    district: str                    # slug
    meeting_date: _dt.date | None
    doc_type: str | None
    doc_title: str
    section_path: str
    heading: str | None
    content: str
    source_url: str
    score: float = 0.0               # RRF score (fused)
    rerank_score: float | None = None


@dataclass
class Panel:
    """Per-district evidence for one question."""

    question: str
    by_district: dict[str, list[EvidenceChunk]]
    empty_districts: list[str] = field(default_factory=list)

    def all_chunks(self) -> list[EvidenceChunk]:
        out: list[EvidenceChunk] = []
        for slug in sorted(self.by_district):
            out.extend(self.by_district[slug])
        return out


# ---- SQL legs ----------------------------------------------------------
# Both legs rank per district via row_number() over (partition by district)
# and share the same filter block, so the panel shape is enforced in SQL.

_FILTERS = """
      and (%(districts)s::text[] is null or d.slug = any(%(districts)s::text[]))
      and (%(doc_type)s::text is null or c.doc_type = %(doc_type)s::text)
      and (%(date_from)s::date is null or c.meeting_date >= %(date_from)s::date)
      and (%(date_to)s::date is null or c.meeting_date <= %(date_to)s::date)
"""

_ROW_COLS = """
    t.id, t.slug, t.meeting_date, t.doc_type, t.title, t.section_path,
    t.heading, t.content, t.source_url
"""


def panel_semantic(
    cur: psycopg.Cursor,
    *,
    query_embedding: list[float],
    per_district: int,
    districts: list[str] | None = None,
    doc_type: str | None = None,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> list[EvidenceChunk]:
    """Top-N chunks per district by cosine distance (ascending)."""
    cur.execute(
        f"""
        select {_ROW_COLS}, t.distance
        from (
            select c.id, d.slug, c.meeting_date, c.doc_type, doc.title,
                   c.section_path, c.heading, c.content, doc.source_url,
                   c.embedding <=> %(qvec)s::vector as distance,
                   row_number() over (
                       partition by c.district_id
                       order by c.embedding <=> %(qvec)s::vector
                   ) as rn
            from chunks c
            join districts d   on d.id = c.district_id
            join documents doc on doc.id = c.document_id
            where c.status = 'active'
              and c.embedding is not null
              {_FILTERS}
        ) t
        where t.rn <= %(per_district)s
        order by t.slug, t.distance
        """,
        {
            "qvec": query_embedding,
            "per_district": per_district,
            "districts": districts,
            "doc_type": doc_type,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return [_row_to_chunk(row) for row in cur.fetchall()]


def panel_fts(
    cur: psycopg.Cursor,
    *,
    query: str,
    per_district: int,
    districts: list[str] | None = None,
    doc_type: str | None = None,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> list[EvidenceChunk]:
    """Top-N chunks per district by full-text rank (descending)."""
    cur.execute(
        f"""
        select {_ROW_COLS}, t.rank
        from (
            select c.id, d.slug, c.meeting_date, c.doc_type, doc.title,
                   c.section_path, c.heading, c.content, doc.source_url,
                   ts_rank_cd(c.fts, q) as rank,
                   row_number() over (
                       partition by c.district_id
                       order by ts_rank_cd(c.fts, q) desc
                   ) as rn
            from chunks c
            join districts d   on d.id = c.district_id
            join documents doc on doc.id = c.document_id,
                 websearch_to_tsquery('english', %(q)s) q
            where c.status = 'active'
              and c.fts @@ q
              {_FILTERS}
        ) t
        where t.rn <= %(per_district)s
        order by t.slug, t.rank desc
        """,
        {
            "q": query,
            "per_district": per_district,
            "districts": districts,
            "doc_type": doc_type,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return [_row_to_chunk(row) for row in cur.fetchall()]


def _row_to_chunk(row) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
        district=row[1],
        meeting_date=row[2],
        doc_type=row[3],
        doc_title=row[4],
        section_path=row[5],
        heading=row[6],
        content=row[7],
        source_url=row[8],
        score=float(row[9]),
    )


def list_district_slugs(cur: psycopg.Cursor) -> list[str]:
    cur.execute("select slug from districts order by slug")
    return [r[0] for r in cur.fetchall()]


# ---- fusion ------------------------------------------------------------

def rrf_fuse_per_district(
    semantic: list[EvidenceChunk],
    fts: list[EvidenceChunk],
    *,
    keep: int,
    k: int = RRF_K,
) -> dict[str, list[EvidenceChunk]]:
    """Reciprocal-rank fusion of the two legs, *within* each district.

    Each leg's rows arrive grouped per district in rank order (that is the
    SQL contract above). A chunk appearing in both legs sums both RRF
    terms. Returns the top ``keep`` per district by fused score.
    """
    def per_district_ranks(rows: list[EvidenceChunk]) -> dict[str, list[EvidenceChunk]]:
        grouped: dict[str, list[EvidenceChunk]] = defaultdict(list)
        for r in rows:
            grouped[r.district].append(r)
        return grouped

    scores: dict[UUID, float] = defaultdict(float)
    best: dict[UUID, EvidenceChunk] = {}
    for leg in (per_district_ranks(semantic), per_district_ranks(fts)):
        for rows in leg.values():
            for rank, chunk in enumerate(rows, start=1):
                scores[chunk.chunk_id] += 1.0 / (k + rank)
                best.setdefault(chunk.chunk_id, chunk)

    fused: dict[str, list[EvidenceChunk]] = defaultdict(list)
    for cid, chunk in best.items():
        chunk.score = scores[cid]
        fused[chunk.district].append(chunk)
    return {
        slug: sorted(rows, key=lambda c: c.score, reverse=True)[:keep]
        for slug, rows in fused.items()
    }


# ---- orchestration -----------------------------------------------------

async def retrieve_panel(
    conn: psycopg.Connection,
    voyage: VoyageEmbedder,
    *,
    question: str,
    reranker: VoyageReranker | None = None,
    per_district: int = DEFAULT_PER_DISTRICT,
    pool: int = DEFAULT_POOL,
    districts: list[str] | None = None,
    doc_type: str | None = None,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
) -> Panel:
    """Retrieve the evidence panel for one question.

    semantic + FTS per-district → RRF per district → (optional) one
    pooled Voyage rerank call → top ``per_district`` per district.
    Districts (after the ``districts`` filter) with no hits are listed in
    ``empty_districts`` — absence is part of the answer.
    """
    qvec = await voyage.embed_query(question)
    cur = conn.cursor()
    filters = dict(districts=districts, doc_type=doc_type,
                   date_from=date_from, date_to=date_to)
    sem = panel_semantic(cur, query_embedding=qvec, per_district=pool, **filters)
    fts = panel_fts(cur, query=question, per_district=pool, **filters)
    # keep the full fused pool per district for the reranker to sift
    fused = rrf_fuse_per_district(sem, fts, keep=pool)

    if reranker is not None:
        pooled = [c for rows in fused.values() for c in rows]
        if pooled:
            results = await reranker.rerank(question, [c.content for c in pooled])
            for res in results:
                pooled[res.index].rerank_score = res.relevance_score
            fused = {
                slug: sorted(
                    rows, key=lambda c: (c.rerank_score or 0.0), reverse=True
                )
                for slug, rows in fused.items()
            }

    by_district = {slug: rows[:per_district] for slug, rows in fused.items() if rows}

    known = list_district_slugs(cur)
    if districts:
        known = [s for s in known if s in districts]
    empty = [s for s in known if s not in by_district]
    return Panel(question=question, by_district=by_district, empty_districts=empty)
