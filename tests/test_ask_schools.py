"""Tests for panel retrieval and the ask layer."""

from __future__ import annotations

import asyncio
import datetime as _dt
from uuid import UUID

from herald.ask_schools import (
    Answer,
    build_user_prompt,
    estimate_cost,
    format_evidence,
    render_markdown,
    validate_citations,
)
from herald.schools_retrieval import (
    EvidenceChunk,
    Panel,
    cap_per_document,
    panel_fts,
    panel_semantic,
    retrieve_panel,
    rrf_fuse_per_district,
)

U1 = UUID("11111111-1111-1111-1111-111111111111")
U2 = UUID("22222222-2222-2222-2222-222222222222")
U3 = UUID("33333333-3333-3333-3333-333333333333")


def _chunk(cid: UUID, district: str, content: str = "Cell phones must be off.",
           source_url: str = "https://d.test/x.pdf") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=cid, district=district, meeting_date=_dt.date(2026, 3, 17),
        doc_type="policy", doc_title="Code of Conduct", section_path="P5.B",
        heading="Electronic Devices", content=content,
        source_url=source_url,
    )


def test_cap_per_document_diversifies_then_backfills():
    a1, a2, a3 = (_chunk(UUID(int=i), "peekskill", source_url="doc-A") for i in (1, 2, 3))
    b1 = _chunk(UUID(int=4), "peekskill", source_url="doc-B")
    # rank order: A,A,A,B — cap 2/doc, limit 3 -> A,A,B (B promoted over 3rd A)
    got = cap_per_document([a1, a2, a3, b1], limit=3, max_per_doc=2)
    assert [c.source_url for c in got] == ["doc-A", "doc-A", "doc-B"]

    # only one document available -> backfill rather than shortchange the slate
    only_a = [_chunk(UUID(int=i), "peekskill", source_url="doc-A") for i in range(1, 5)]
    got = cap_per_document(only_a, limit=3, max_per_doc=2)
    assert len(got) == 3 and all(c.source_url == "doc-A" for c in got)


# ---- fusion ------------------------------------------------------------

def test_rrf_fuse_per_district_sums_legs_and_ranks():
    sem = [_chunk(U1, "peekskill"), _chunk(U2, "peekskill")]
    fts = [_chunk(U2, "peekskill"), _chunk(U3, "ossining")]
    fused = rrf_fuse_per_district(sem, fts, keep=2)
    peek = fused["peekskill"]
    # U2 appears in both legs -> outranks U1 despite worse semantic rank
    assert [c.chunk_id for c in peek] == [U2, U1]
    assert fused["ossining"][0].chunk_id == U3
    # fusion never crosses districts
    assert all(c.district == "peekskill" for c in peek)


def test_rrf_keep_caps_per_district():
    sem = [_chunk(UUID(int=i), "elmsford") for i in range(1, 6)]
    fused = rrf_fuse_per_district(sem, [], keep=3)
    assert len(fused["elmsford"]) == 3


# ---- SQL shapes --------------------------------------------------------

class FakeCursor:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows


def _row(cid, slug):
    return (cid, slug, _dt.date(2026, 3, 17), "policy", "Code of Conduct",
            "P5.B", "Electronic Devices", "Cell phones must be off.",
            "https://d.test/x.pdf", 0.31)


def test_panel_semantic_sql_partitions_by_district():
    cur = FakeCursor(rows=[_row(U1, "peekskill")])
    out = panel_semantic(cur, query_embedding=[0.0] * 4, per_district=5)
    sql, params = cur.calls[0]
    assert "row_number() over ( partition by c.district_id" in sql
    assert "c.status = 'active'" in sql and "embedding is not null" in sql
    assert params["per_district"] == 5
    assert out[0].chunk_id == U1 and out[0].district == "peekskill"
    assert out[0].doc_title == "Code of Conduct"


def test_panel_fts_sql_partitions_and_filters():
    cur = FakeCursor(rows=[_row(U2, "ossining")])
    out = panel_fts(cur, query="cell phone policy", per_district=8,
                    districts=["ossining"], doc_type="policy")
    sql, params = cur.calls[0]
    assert "websearch_to_tsquery" in sql
    assert "partition by c.district_id" in sql
    assert params["districts"] == ["ossining"] and params["doc_type"] == "policy"
    assert out[0].district == "ossining"


# ---- retrieve_panel orchestration -------------------------------------

class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class PanelCursor(FakeCursor):
    """Returns chunk rows for the two panel queries, slugs for the district list."""

    def __init__(self, rows, slugs):
        super().__init__(rows)
        self._slugs = slugs

    def fetchall(self):
        sql = self.calls[-1][0]
        if "from districts" in sql and "select slug" in sql:
            return [(s,) for s in self._slugs]
        return self._rows


class FakeVoyage:
    async def embed_query(self, text):
        return [0.0] * 4


class FakeReranker:
    def __init__(self):
        self.called_with = None

    async def rerank(self, query, documents, *, top_k=None):
        from herald.rerank import RerankResult

        self.called_with = (query, list(documents))
        # reverse order, so rerank visibly changes ranking
        n = len(documents)
        return [RerankResult(index=i, relevance_score=float(i)) for i in range(n)]


def test_retrieve_panel_reports_empty_districts_and_reranks():
    rows = [_row(U1, "peekskill"), _row(U2, "peekskill")]
    cur = PanelCursor(rows, slugs=["ossining", "peekskill"])
    rr = FakeReranker()
    panel = asyncio.run(retrieve_panel(
        FakeConn(cur), FakeVoyage(), question="cell phone policy",
        reranker=rr, per_district=1, pool=5,
    ))
    assert panel.empty_districts == ["ossining"]        # absence surfaced
    assert list(panel.by_district) == ["peekskill"]
    assert len(panel.by_district["peekskill"]) == 1     # per_district cap
    # reranker scores flipped the order: U2 (index 1) wins
    assert panel.by_district["peekskill"][0].chunk_id == U2
    assert rr.called_with[0] == "cell phone policy"


# ---- prompt/citation/rendering ----------------------------------------

def _panel():
    return Panel(
        question="What's the normal cell phone policy?",
        by_district={
            "ossining": [_chunk(U3, "ossining", "Phones allowed at lunch.")],
            "peekskill": [_chunk(U1, "peekskill")],
        },
        empty_districts=["elmsford"],
    )


def test_format_evidence_groups_numbers_and_lists_empty():
    text, ordered = format_evidence(_panel())
    assert text.index("### District: ossining") < text.index("### District: peekskill")
    assert "[1]" in text and "[2]" in text
    assert [c.chunk_id for c in ordered] == [U3, U1]    # [N] order matches list
    assert "NO retrieved evidence" in text and "elmsford" in text


def test_build_user_prompt_carries_question():
    prompt, ordered = build_user_prompt(_panel())
    assert "What's the normal cell phone policy?" in prompt
    assert len(ordered) == 2


def test_validate_citations():
    assert validate_citations("Fine [1] and [2].", 2) == []
    assert validate_citations("Bad [3] worse [12].", 2) == [3, 12]
    assert validate_citations("No citations at all.", 2) == []


def test_estimate_cost():
    # sonnet-5 intro pricing: $2/M in, $10/M out
    assert estimate_cost("claude-sonnet-5", 16_000, 6_000) == 0.032 + 0.060
    assert estimate_cost("something-unknown", 1000, 1000) is None


def test_render_markdown_includes_evidence_and_absence():
    _, ordered = format_evidence(_panel())
    ans = Answer(text="Phones are banned in class [2], allowed at lunch in "
                      "Ossining [1].", panel=_panel(), evidence=ordered,
                 model="claude-sonnet-5")
    md = render_markdown(ans)
    assert "## Evidence" in md
    assert "peekskill · 2026-03-17" in md and "ossining · 2026-03-17" in md
    assert "No evidence retrieved from: elmsford" in md
