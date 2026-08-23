"""Tests for reading a timeframe out of a question.

The live failure: *"How much did each district pay Google last year?"* was
answered from evidence dated 2013 to 2026. `--since` was empty and nothing
looked at the question, so "last year" was silently dropped.
"""

from __future__ import annotations

import datetime as _dt

from herald.timeframe import Timeframe, detect_timeframe, scope_note, span_of


def test_a_school_year_is_july_to_june_not_a_calendar_year():
    # District documents are organised on the school year, so "2023-24" and
    # "2023" are different windows. Treating them alike would quietly pull in
    # six months of the wrong year.
    f = detect_timeframe("Show me the 2023-24 school year budget")
    assert f.date_from == _dt.date(2023, 7, 1)
    assert f.date_to == _dt.date(2024, 6, 30)

    cal = detect_timeframe("What did Ossining spend in 2023?")
    assert cal.date_from == _dt.date(2023, 1, 1)
    assert cal.date_to == _dt.date(2023, 12, 31)


def test_school_year_accepts_the_forms_districts_actually_write():
    for text in ("2023-24", "2023-2024", "2023/24", "2023 - 24", "2023\u20132024"):
        f = detect_timeframe(f"the {text} budget")
        assert f.date_from == _dt.date(2023, 7, 1), text
        assert f.date_to == _dt.date(2024, 6, 30), text


def test_open_ended_ranges():
    since = detect_timeframe("board decisions since 2022")
    assert since.date_from == _dt.date(2022, 1, 1) and since.date_to is None

    before = detect_timeframe("anything before 2020")
    assert before.date_to == _dt.date(2019, 12, 31) and before.date_from is None


def test_a_timeless_question_gets_no_frame():
    assert detect_timeframe("What is the attendance policy?") is None
    assert detect_timeframe("") is None


def test_vague_expressions_are_detected_but_never_resolved():
    # "last year" in August could be the 2025 calendar year or the 2025-26
    # school year. Guessing silently is the mistake this project keeps
    # finding; the caller must surface it instead.
    for q in ("How much did each district pay Google last year?",
              "the most recent contract",
              "what have they done recently",
              "this school year"):
        f = detect_timeframe(q)
        assert f is not None, q
        assert not f.resolved, q
        assert f.date_from is None and f.date_to is None, q


def test_span_of_ignores_undated_evidence():
    dates = [None, _dt.date(2019, 4, 9), None, _dt.date(2026, 2, 4), _dt.date(2013, 1, 30)]
    assert span_of(dates) == (_dt.date(2013, 1, 30), _dt.date(2026, 2, 4))
    assert span_of([None, None]) is None
    assert span_of([]) is None


def test_scope_note_says_plainly_when_time_was_not_applied():
    note = scope_note(
        Timeframe("last year"), applied=False,
        evidence_span=(_dt.date(2013, 1, 30), _dt.date(2026, 2, 4)),
    )
    assert "Not scoped in time" in note
    assert "last year" in note
    assert "2013-01-30 to 2026-02-04" in note      # the decade, stated outright
    assert "since" in note                          # and how to fix it


def test_scope_note_confirms_a_frame_that_was_applied():
    note = scope_note(
        Timeframe("in 2024", _dt.date(2024, 1, 1), _dt.date(2024, 12, 31)),
        applied=True, evidence_span=(_dt.date(2024, 3, 1), _dt.date(2024, 3, 1)),
    )
    assert note.startswith("_Scoped to")
    assert "2024-01-01 .. 2024-12-31" in note
    # a single-date span reads "dated X.", not "dated X to X"
    span = note.split("Evidence shown is ", 1)[1]
    assert span == "dated 2024-03-01."


def test_a_timeless_question_gets_no_note_at_all():
    assert scope_note(None, applied=False, evidence_span=None) == ""
