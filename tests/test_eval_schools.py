"""Tests for the corpus eval grader.

The grader is pure — it takes retrieved text, not a database — so the rules
that decide pass and fail can be pinned without a corpus.
"""

from __future__ import annotations

from herald.eval_schools import (
    DEFAULT_CASES,
    EvalCase,
    Expectation,
    grade_case,
    load_cases,
    normalize,
    render_report,
)

ATED = ("At the high school level, any student with more than nine unexcused ATEDs "
        "for one-half year or 18 unexcused ATEDs for a full year will not receive "
        "credit for that course.")


def _case(**kw) -> EvalCase:
    base = dict(
        id="attendance", question="q", kind="coverage",
        expect_present=(Expectation("tarrytowns", ("18 unexcused", "will not receive credit")),),
    )
    base.update(kw)
    return EvalCase(**base)


def test_a_district_that_returned_the_passage_passes():
    r = grade_case(_case(), {"tarrytowns": [ATED]})
    assert r.passed and r.results[0].detail == "ok"


def test_retrieving_nothing_for_the_district_fails_loudly():
    # The original bug: the answer layer said "only Tarrytowns" because
    # nothing came back for anyone else. Silence must read as failure.
    r = grade_case(_case(), {})
    assert not r.passed
    assert r.results[0].detail == "no evidence retrieved for this district"


def test_retrieving_the_wrong_passage_is_distinguished_from_retrieving_nothing():
    r = grade_case(_case(), {"tarrytowns": ["Students shall attend school regularly."]})
    assert not r.passed
    assert "missing" in r.results[0].detail
    assert r.results[0].chunks_seen == 1


def test_every_required_string_must_appear():
    # Half a match is a false pass: "18 unexcused" alone could come from a
    # sentence about notification, not credit denial.
    r = grade_case(_case(), {"tarrytowns": ["after 18 unexcused absences a letter is mailed"]})
    assert not r.passed
    assert r.results[0].missing == ["will not receive credit"]


def test_matching_survives_nbsp_and_line_wrapping():
    # Real policy text carries non-breaking spaces and hard wraps; a match
    # lost to formatting would look exactly like a missing document.
    wrapped = ATED.replace(" ", "\u00a0", 3).replace("18 unexcused", "18\n  unexcused")
    r = grade_case(_case(), {"tarrytowns": [wrapped]})
    assert r.passed


def test_normalize_is_case_and_space_insensitive():
    assert normalize("Home\u00a0Schooled   STUDENTS\n") == "home schooled students"


def test_a_negative_control_fails_when_a_district_answers_anyway():
    case = _case(id="swimming", kind="negative", expect_present=(), expect_no_evidence=True)
    assert grade_case(case, {}).passed
    bad = grade_case(case, {"ossining": ["All students must pass a swim test."]})
    assert not bad.passed
    assert bad.unexpected_evidence == ["ossining"]


def test_a_negative_control_ignores_districts_that_returned_empty_lists():
    case = _case(id="swimming", kind="negative", expect_present=(), expect_no_evidence=True)
    assert grade_case(case, {"ossining": [], "peekskill": []}).passed


def test_no_known_rule_districts_are_never_graded_as_a_required_no():
    # "No matching sentence in what we hold" is not "no such rule". Grading it
    # as a required absence would punish the corpus for growing.
    case = _case(no_known_rule=("white-plains",))
    r = grade_case(case, {"tarrytowns": [ATED], "white-plains": ["Some attendance text."]})
    assert r.passed
    assert len(r.results) == 1          # only the presence expectation was graded


def test_report_puts_failures_first_and_explains_them():
    passing = grade_case(_case(id="ok-case"), {"tarrytowns": [ATED]})
    failing = grade_case(_case(id="broken-case", why="the policy gap"), {})
    md = render_report([passing, failing])
    assert md.index("broken-case") < md.index("ok-case")
    assert "1/2 cases passed" in md
    assert "the policy gap" in md
    assert "no evidence retrieved" in md


def test_the_shipped_case_file_loads_and_is_well_formed():
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 5
    ids = {c.id for c in cases}
    assert "attendance-credit-threshold" in ids
    # a suite with no negative control trains the system to confabulate
    assert any(c.expect_no_evidence for c in cases)
    for c in cases:
        assert c.question.strip(), c.id
        assert c.why.strip(), f"{c.id} must say why it exists"
        for e in c.expect_present:
            assert e.must_match, f"{c.id}/{e.district} has nothing to match"
            assert all(m.strip() for m in e.must_match), c.id
