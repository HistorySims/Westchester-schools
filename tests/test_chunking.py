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


def test_budget_and_transcript_are_classifiable_at_all():
    # Both are DocType members, but the classifier had no rule for either, so
    # 118 documents with "budget" in the title sat in 'other' — invisible to a
    # doc_type='budget' filter. A type you can store but never derive is a
    # silent hole in every coverage answer.
    assert classify_doc_type("2026-2027 Adopted Budget") == "budget"
    assert classify_doc_type("Property Tax Report Card 2026-27") == "budget"
    assert classify_doc_type("Board Meeting Transcript 3-12-26") == "transcript"
    # "Budget Presentation" and "Financial Statement" used to land here for
    # want of anywhere better. They now have their own types — see
    # test_a_slide_deck_is_a_presentation_not_a_budget and
    # test_financial_reports_are_not_budgets_and_not_other.
    assert classify_doc_type("Budget Presentation (Spanish)") == "presentation"
    assert classify_doc_type("Financial Statement FY25") == "financial"


def test_a_policy_about_budgets_is_still_a_policy():
    assert classify_doc_type("Budget Adoption Policy") == "policy"
    assert classify_doc_type("6700-R Purchasing Regulation") == "policy"


def test_a_transcript_beats_the_meeting_keywords():
    # "board meeting" would otherwise make this an agenda.
    assert classify_doc_type("Regular Board Meeting Transcript") == "transcript"


def test_audits_are_not_folded_into_budget():
    # An audit is a financial document but not a spending plan; counting it as
    # a budget makes "show me the budget" return audit reports. They used to
    # fall to 'other' for want of anywhere better — the point was always that
    # they are NOT budgets, and that still holds.
    for title in ("Independent Auditor Report 2025", "Claims Audit Report"):
        assert classify_doc_type(title) != "budget"
        assert classify_doc_type(title) == "financial"


def test_a_cba_named_after_a_union_acronym_is_still_a_contract():
    # "Tarrytown-TAT-2022-2025.pdf" matches no keyword — no rule can be
    # expected to know TAT is the Tarrytown Association of Teachers. Judged on
    # the filename alone it stayed 'other', invisible to every
    # doc_type='contract' filter, while its salary grid sat in the corpus.
    url = "https://tarrytownlearningcenter.org/wp-content/uploads/contracts/tat.pdf"
    assert classify_doc_type("Tarrytown-TAT-2022-2025.pdf") == "other"
    assert classify_doc_type("Tarrytown-TAT-2022-2025.pdf", url) == "contract"


def test_the_url_test_is_narrower_than_the_title_test():
    # A path segment is structural evidence; prose vocabulary in a URL is not.
    # A newsletter ABOUT negotiations must not be filed as a contract.
    assert classify_doc_type("Some Newsletter",
                             "https://x.org/news/contract-negotiations-update") == "other"
    assert classify_doc_type("Board Update",
                             "https://x.org/news/budget-vote-results") == "other"
    assert classify_doc_type("2026-27 Overview",
                             "https://x.org/district/budget/overview.pdf") == "budget"


def test_the_title_still_wins_over_the_url():
    # The URL is a fallback, consulted only when the title says nothing.
    assert classify_doc_type("January 2024 Minutes",
                             "https://x.org/contracts/jan.pdf") == "minutes"


def test_both_classifiers_share_one_contract_vocabulary():
    # They drifted: site.py knew "collective bargaining" and "federation of
    # teachers" meant a contract and the ingest-time classifier did not, so
    # anything reaching ingest as 'other' stayed 'other'.
    from herald.scrape.models import DocType
    from herald.scrape.site import classify_link

    for title in ("Collective Bargaining Agreement 2022-25", "TAT Salary Schedule",
                  "Federation of Teachers contract", "Memorandum of Agreement"):
        assert classify_doc_type(title) == "contract", title
        assert classify_link("https://x.org/a.pdf", title) is DocType.contract, title


def test_the_boarddocs_minutes_abbreviation_is_recognised():
    # Real titles from the corpus: BoardDocs names minutes attachments
    # "Min 4.18.23.pdf". Ninety Tarrytowns meeting records sat in 'other'
    # because "min" is not "minute", so the whole meeting record was
    # unfilterable.
    for title in ("Min 1.11.24.pdf (24,619 KB)", "Min 4.18.23.pdf (1,445 KB)",
                  "Mins 9.5.24.pdf", "Min. 3.2.23.pdf"):
        assert classify_doc_type(title) == "minutes", title


def test_the_abbreviation_cannot_reach_ordinary_words():
    # It is anchored and requires the date that follows, so a bare "min"
    # prefix is not enough — otherwise every minimum, mini-grant and minority
    # report in the corpus becomes a set of meeting minutes.
    for title in ("minimum attendance requirements", "Mini-Grant Award 2024",
                  "Ministry of Education report", "Minority Business Enterprise Report",
                  "Minnesota comparison study"):
        assert classify_doc_type(title) == "other", title


def test_a_slide_deck_is_a_presentation_not_a_budget():
    # Ossining's "Directors' Budget Presentation" is 56 pages of which 46 are
    # flat images, and its text is promotional. Typed 'budget' it competed
    # with the actual budget books: a question about a spending line could
    # retrieve a slide reading "An increase in teaching salaries" instead of
    # the book line carrying the figure. Genre, not subject.
    assert classify_doc_type("Directors' Budget Presentation") == "presentation"
    assert classify_doc_type("Budget Hearing Presentation 5.7.26.pdf") == "presentation"
    assert classify_doc_type("Educational Plan & Budget Workshop #2 (English)") == "presentation"
    assert classify_doc_type("TUFSD BOE Spring 23' Data Dive _ 3.23.23.pdf") == "presentation"

    # ...and it is checked BEFORE the meeting keywords, or "BOE Meeting
    # Presentation" matches "boe meeting" and lands in 'agenda'.
    assert classify_doc_type("OUFSD - BOE Meeting Presentation_FINAL.pdf") == "presentation"

    # An actual budget is still a budget.
    assert classify_doc_type("2026-2027 Proposed Budget") == "budget"
    assert classify_doc_type("Budget Adoption Policy") == "policy"


def test_financial_reports_are_not_budgets_and_not_other():
    # 43 Treasurer's Reports sat in 'other' — the bucket nothing filters on —
    # because they are financial but are not spending plans, and folding them
    # into 'budget' would make "show me the budget" return audit reports.
    assert classify_doc_type("Treasurer's Report - March 2026") == "financial"
    assert classify_doc_type("Warrant Register 04-2026") == "financial"
    assert classify_doc_type("Claims Auditor Report") == "financial"
    assert classify_doc_type("External Audit Report 2024-25") == "financial"
    assert classify_doc_type("Extraclassroom Activity Funds") == "financial"


def test_financial_yields_to_the_document_a_title_actually_names():
    # Checked after 'agenda' and 'minutes' so the meeting record about the
    # audit stays a meeting record.
    assert classify_doc_type("Audit Committee Agenda") == "agenda"
    assert classify_doc_type("Audit Minutes") == "minutes"
    # 'audit' must not reach "auditorium"
    assert classify_doc_type("Auditorium Use Agreement") == "contract"
