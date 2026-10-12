"""Tests for comparing one board policy across districts.

The clauses quoted here are real, from `data/snapshots/boarddocs-policies.jsonl.gz`.
They are the measured cases: what the tool found, and what it has to keep
finding.
"""

from __future__ import annotations

import re

from herald.policy_compare import (
    Quantity,
    clauses,
    divergences,
    policy_number,
    quantities,
    rule_parameter,
    wording_key,
)

# Policy 1050, verbatim from Tarrytowns and White Plains. One sentence, one
# figure, and about a month of difference in who can reach the ballot.
ELECTION_A = ("Such petitions must be submitted 90 days in advance of the annual "
              "election to facilitate the preparation and printing of the ballots.")
ELECTION_B = ("Such petitions must be submitted 60 days in advance of the annual "
              "election to facilitate the preparation and printing of the ballots.")

# Policy 7613. Same number, different unit — the easiest kind to read past.
PLACEMENT_A = ("However, if such recommendation of the CSE is for placement in an "
               "approved in-state or out-of-state private school, the Board shall "
               "arrange for such special education programs and services for students "
               "with disabilities within thirty (30) school days of the Board's receipt.")
PLACEMENT_B = ("However, if such recommendation of the CSE is for placement in an "
               "approved in-state or out-of-state private school, the Board shall "
               "arrange for such special education programs and services for students "
               "with disabilities within thirty (30) days of the Board's receipt.")


class TestQuantities:
    def test_a_quantity_needs_a_unit(self) -> None:
        """Citations are numbers too, and there are far more of them.

        Comparing bare numbers buried every real finding under citation
        formatting: "8 N.Y.C.R.R. 200.2(c)" against "(8NYCRR 200.2(c))" is one
        citation tokenized two ways. Requiring a unit drops all of it with no
        special case for citations.
        """
        assert quantities("in conformance with Commissioner's Regulations "
                          "(8NYCRR 200.2(c)).") == ()
        assert quantities("adopts the NIST Cybersecurity Framework Version 2.0") == ()
        assert quantities("As required by Public Officers Law 87, the district") == ()

    def test_a_spelled_number_is_read_from_its_figures(self) -> None:
        """These policies write "eighteen (18) absences"; the figure is the value."""
        assert Quantity(18, "absence") in quantities(
            "Class credit will not be given when a student exceeds eighteen (18) "
            "absences in a full credit course")

    def test_the_unit_qualifier_is_kept(self) -> None:
        assert quantities(PLACEMENT_A) == (Quantity(30, "school day"),)
        assert quantities(PLACEMENT_B) == (Quantity(30, "day"),)

    def test_nys_term_of_art_counts_as_a_unit(self) -> None:
        """Tarrytowns states its whole attendance rule in ATEDs.

        Absence, Tardiness or Early Departure. Omitting the unit read that
        district as having no credit-denial rule when it has an explicit 18.
        """
        found = quantities(
            "any student with more than nine unexcused ATEDs for one-half year or "
            "18 unexcused ATEDs for a full year will not receive credit")
        assert Quantity(18, "ated") in found
        # the unit compares lowercased but has to read correctly in a report
        assert str(Quantity(18, "ated")) == "18 ATEDs"


class TestWordingKey:
    def test_two_clauses_differing_only_in_a_number_have_one_key(self) -> None:
        assert wording_key(ELECTION_A) == wording_key(ELECTION_B)

    def test_a_spelled_figure_does_not_change_the_key(self) -> None:
        assert wording_key("less than 12 months for which") == wording_key(
            "less than six (6) months for which")


class TestDivergences:
    def _find(self, a: str, b: str, **kw):
        return divergences("1050", "Annual District Election",
                           "tarrytowns", clauses(a), "white-plains", clauses(b), **kw)

    def test_the_same_sentence_with_a_different_number_is_found(self) -> None:
        found = self._find(ELECTION_A, ELECTION_B)
        assert len(found) == 1
        assert found[0].wording == 1.0
        assert found[0].kind == "value"
        assert found[0].quantities_a == (Quantity(90, "day"),)
        assert found[0].quantities_b == (Quantity(60, "day"),)

    def test_the_same_number_with_a_different_unit_is_also_found(self) -> None:
        found = divergences("7613", "Role of the Board", "greenburgh-central",
                            clauses(PLACEMENT_A), "mount-vernon", clauses(PLACEMENT_B))
        assert len(found) == 1
        assert found[0].kind == "unit"

    def test_identical_policy_text_yields_nothing(self) -> None:
        assert self._find(ELECTION_A, ELECTION_A) == []

    def test_unrelated_clauses_are_not_paired(self) -> None:
        other = ("The Superintendent will report annually to the Board on the "
                 "condition of each school building within 45 days of the inspection.")
        assert self._find(ELECTION_A, other) == []

    def test_a_clause_with_no_quantity_is_skipped(self) -> None:
        bare = ("The Board of Education recognizes an important relationship between "
                "class attendance and student performance in every subject offered.")
        assert self._find(bare, bare.replace("every", "each")) == []


class TestRuleParameter:
    DENIAL = re.compile(r"may\s+not\s+receive\s+credit|will\s+not\s+be\s+given", re.I)

    # Greenburgh's real ladder, compressed: the rungs before the denial are the
    # trap. A parent call at 4, a letter at 9, a contract at 13, denial at 18.
    LADDER = (
        "When a student accrues four (4) absences in a full credit course the teacher "
        "will contact the parent. When a student accrues nine (9) absences in a full "
        "credit course the Dean of Students will send a letter. When a student accrues "
        "thirteen (13) absences in a full credit course an Attendance Contract is "
        "generated. Class credit will not be given when a student exceeds eighteen "
        "(18) absences in a full credit course or nine (9) absences in a half credit "
        "course."
    )

    def test_the_denial_rung_is_read_not_an_earlier_one(self) -> None:
        got = rule_parameter("greenburgh-central", "7110", self.LADDER,
                             consequence=self.DENIAL, units=("absence", "day", "ATED"))
        assert got is not None
        assert got.value == 18
        assert not got.is_discretionary

    def test_a_rewritten_clause_is_still_read(self) -> None:
        """Why this exists: sentence alignment cannot pair these two.

        Greenburgh writes a sentence, Mount Vernon writes a bulleted list, and
        the broad sweep misses the best-known parameter in the corpus.
        """
        mount_vernon = ("the following guidelines apply to student receiving no "
                        "credit: 10 absences for a 1/2 credit course 20 absences for "
                        "a one (1) credit course")
        got = rule_parameter("mount-vernon", "7110", mount_vernon,
                             consequence=re.compile(r"no\s+credit", re.I),
                             units=("absence",))
        assert got is not None and got.value == 20

    def test_a_policy_that_sets_no_number_reports_none(self) -> None:
        """White Plains leaves it to an administrator; that is not a missing rule."""
        white_plains = ("The decision to deny course credit to a student will be made "
                        "by the House Administrator. If dissatisfied, the student "
                        "and/or parent may appeal to the High School Principal within "
                        "five (5) school days after the date of the decision.")
        got = rule_parameter("white-plains", "5100", white_plains,
                             consequence=re.compile(r"deny\s+course\s+credit", re.I),
                             units=("absence", "ATED"))
        assert got is None

    def test_an_appeal_deadline_is_not_the_threshold(self) -> None:
        """The unit filter is what keeps the five-day appeal clock out of it."""
        got = rule_parameter("white-plains", "5100",
                             "credit will not be given; appeal within 5 school days",
                             consequence=re.compile(r"credit\s+will\s+not\s+be\s+given"),
                             units=("absence", "ATED"))
        assert got is None


def test_policy_number_is_read_off_the_title() -> None:
    assert policy_number("7110 Comprehensive Student Attendance Policy") == "7110"
    assert policy_number("5100 ATTENDANCE") == "5100"
    assert policy_number("6700-R Purchasing Regulation") == "6700-R"
    assert policy_number("4531-E.7 FIELD TRIP ATTENDANCE SHEET") == "4531-E.7"
    assert policy_number("Forward") == ""


class TestCompetingRulesInOneParagraph:
    """A policy paragraph states several rules; the biggest number may be another's.

    Mount Vernon's attendance policy puts "a student with 20 consecutive
    absences ... can be discharged" two sentences from its "no credit"
    language. Taking the largest nearby figure reported the discharge rule and
    quoted the discharge sentence as evidence for a credit threshold — and the
    discharge figure happens to equal the real credit threshold, so the output
    looked right while its provenance was wrong.
    """

    MOUNT_VERNON = (
        "Ten (10) Absences - Final parent/guardian letter regarding potential for "
        "student receiving no credit status for the course. If a student is 16 years "
        "of age or older with 20 consecutive absences not accompanied with a note, "
        "student can be discharged."
    )
    DENIAL = re.compile(r"no\s+credit", re.I)
    COMPETING = re.compile(r"discharg|CPS|summer\s+school|suspen|truan", re.I)

    def test_without_the_exclusion_the_wrong_rule_wins(self) -> None:
        got = rule_parameter("mount-vernon", "7110", self.MOUNT_VERNON,
                             consequence=self.DENIAL, units=("absence",))
        assert got is not None and got.value == 20   # the discharge figure

    def test_the_exclusion_drops_it(self) -> None:
        got = rule_parameter("mount-vernon", "7110", self.MOUNT_VERNON,
                             consequence=self.DENIAL, units=("absence",),
                             exclude=self.COMPETING)
        # 10 is the only credit figure in this excerpt; 20 belongs to discharge
        assert got is not None and got.value == 10

    def test_the_real_credit_clause_survives_the_exclusion(self) -> None:
        real = ("the following guidelines apply to student receiving no credit: "
                "10 absences for a 1/2 credit course 20 absences for a one (1) "
                "credit course")
        got = rule_parameter("mount-vernon", "7110", real,
                             consequence=self.DENIAL, units=("absence",),
                             exclude=self.COMPETING)
        assert got is not None and got.value == 20

    def test_a_parameter_renders_as_the_policy_states_it(self) -> None:
        got = rule_parameter("tarrytowns", "5100",
                             "any student with more than 18 unexcused ATEDs for a "
                             "full year will not receive credit for that course",
                             consequence=re.compile(r"not\s+receive\s+credit", re.I),
                             units=("ated",))
        assert got is not None and str(got) == "18 ATEDs"
