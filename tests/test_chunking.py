"""Tests for the structural agenda chunker."""

from __future__ import annotations

from datetime import date

from herald.chunking import (
    MAX_CHARS,
    Chunk,
    chunk_agenda_text,
    classify_doc_type,
    parse_meeting_date,
)

AGENDA = """\
Peekskill City School District
BUSINESS MEETING
MARCH 17, 2026

1. Call to Order
The meeting was called to order at 6:10 p.m.

2. Hearing of Citizens
No speakers.

3. Consent Agenda - Business/Finance
A. Budget Appropriation Transfers - February 2026
That the Board approves the transfers for February 2026.
D. Southern Westchester BOCES Cooperative Bid 2026/2027
That the Board approves the cooperative bid award for the 2026/2027 year.
"""


def test_parse_meeting_date_from_header():
    assert parse_meeting_date(AGENDA) == date(2026, 3, 17)
    assert parse_meeting_date("no date here") is None


def test_classify_doc_type_handles_separators():
    assert classify_doc_type("Business-Meeting-March-17-2026.pdf") == "agenda"
    assert classify_doc_type("2025 Approved Minutes.pdf") == "minutes"
    assert classify_doc_type("Policy 5030 Wellness") == "policy"
    assert classify_doc_type("Student Handbook") == "handbook"
    assert classify_doc_type("random.pdf") == "other"


def test_narrative_sections_become_one_chunk_each():
    chunks = chunk_agenda_text(AGENDA, district="peekskill")
    paths = [c.section_path for c in chunks]
    # header + P1 + P2 stay whole
    assert "P1" in paths and "P2" in paths
    p2 = next(c for c in chunks if c.section_path == "P2")
    assert p2.section_type == "Hearing of Citizens"
    assert "No speakers" in p2.content


def test_consent_agenda_splits_per_lettered_item():
    chunks = chunk_agenda_text(AGENDA, district="peekskill")
    finance = {
        c.section_path: c
        for c in chunks
        if c.section_type == "Consent Agenda - Business/Finance"
    }
    assert "P3.A" in finance and "P3.D" in finance   # one chunk per action
    assert "BOCES" in finance["P3.D"].content
    assert finance["P3.D"].heading.startswith("Southern Westchester BOCES")


def test_doc_metadata_is_stamped_on_every_chunk():
    chunks = chunk_agenda_text(
        AGENDA, district="peekskill", meeting_date=date(2026, 3, 17),
        doc_type="agenda", source_url="http://x/y.pdf",
    )
    assert chunks and all(
        c.district == "peekskill" and c.meeting_date == date(2026, 3, 17) for c in chunks
    )
    # order_index is dense and increasing
    assert [c.order_index for c in chunks] == list(range(len(chunks)))


def test_oversize_item_is_split_with_unique_paths():
    big = "1. Long Narrative Section\n" + ("word " * 4000)
    chunks = chunk_agenda_text(big, district="d")
    assert len(chunks) > 1
    assert all(len(c.content) <= MAX_CHARS + 50 for c in chunks)
    # split pieces carry unique #-suffixed paths
    assert len({c.section_path for c in chunks}) == len(chunks)


def test_returns_chunk_objects():
    chunks = chunk_agenda_text(AGENDA)
    assert all(isinstance(c, Chunk) for c in chunks)
