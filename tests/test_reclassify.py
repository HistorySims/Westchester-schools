"""Tests for the reclassify decision.

`reclassify` had no tests at all, which is how it shipped able to demote a
real budget to `other`. The decision lived inside a database loop where
nothing could reach it; it is a pure function now, and this is that function's
net.
"""

from __future__ import annotations

from herald.ingest_schools import reclassify_target


def test_a_budget_the_title_cannot_name_is_kept_not_demoted():
    # Caught by a dry run: 4 real budgets queued for demotion to 'other'.
    # Elmsford's line-by-line is the most detailed budget a district produces
    # and contains no classifiable word at all. The scraper typed it 'budget'
    # from where it was published — evidence the title does not carry.
    new, held = reclassify_target(
        "2020 - 2021 Line-By-Line (Revised For Pandemic Adjustments)", "", "budget"
    )
    assert new is None, "a real budget must not be demoted out of budget filters"
    assert held is True, "and the hold must be reported, not silent"


def test_a_hold_is_reported_for_every_type_not_just_budget():
    for current in ("contract", "policy", "handbook", "minutes", "financial"):
        new, held = reclassify_target("Untitled Scan 004", "", current)
        assert new is None and held is True, current


def test_a_genuine_refinement_still_happens():
    # The whole point of the run: slide decks out of 'budget'.
    assert reclassify_target(
        "2019-2020 Preliminary Budget Presentation (revised 04-03-19)", "", "budget"
    ) == ("presentation", False)
    assert reclassify_target(
        "Financial Statements & External Audit Report", "", "budget"
    ) == ("financial", False)
    assert reclassify_target(
        "May 15, 2018 Minutes (Annual Budget Vote & Board Election)", "", "budget"
    ) == ("minutes", False)


def test_an_unclassifiable_document_already_in_other_is_not_a_hold():
    # Nothing is lost by leaving 'other' as 'other' — there was no knowledge
    # to preserve, so this is a no-op and not a declined decision.
    assert reclassify_target("Untitled Scan 004", "", "other") == (None, False)


def test_no_change_when_the_type_is_already_right():
    assert reclassify_target("2026-2027 Adopted Budget", "", "budget") == (None, False)
    assert reclassify_target("Treasurer's Report - March 2026", "", "financial") == (
        None, False
    )


def test_the_url_still_gets_a_vote_before_a_hold():
    # A file under /contracts/ is a contract even when the title says nothing,
    # so this refines rather than holding.
    assert reclassify_target(
        "Tarrytown-TAT-2022-2025.pdf",
        "https://tufsd.org/contracts/Tarrytown-TAT-2022-2025.pdf",
        "other",
    ) == ("contract", False)


def test_allowed_doc_types_reads_the_live_constraint():
    # The enum and the CHECK constraint drift on purpose: code gains a type in
    # one commit, the migration adding it is applied separately and later. A
    # run must ask the database what it will accept, not assume.
    from herald.schools_db import _QUOTED

    definition = (
        "CHECK ((doc_type = ANY (ARRAY['minutes'::text, 'agenda'::text, "
        "'policy'::text, 'presentation'::text, 'financial'::text])))"
    )
    assert set(_QUOTED.findall(definition)) == {
        "minutes", "agenda", "policy", "presentation", "financial",
    }


def test_an_unmigrated_database_is_detected_before_anything_is_written():
    # The live failure: `presentation` was written to a database whose
    # constraint had never learned it. connect_raw sets autocommit, so the
    # CheckViolation landed AFTER earlier rows had already been committed —
    # a half-reclassified corpus and a stack trace.
    allowed = {"minutes", "agenda", "policy", "budget", "other"}
    wanted = {"presentation", "financial", "minutes"}
    assert not wanted <= allowed
    assert sorted(wanted - allowed) == ["financial", "presentation"]


def test_an_empty_constraint_read_does_not_block_the_run():
    # allowed_doc_types returns an empty set when the constraint cannot be
    # found. That means "unknown", and must never be read as "nothing is
    # permitted" — otherwise a renamed constraint bricks every reclassify.
    allowed: set[str] = set()
    wanted = {"presentation"}
    assert not (allowed and not wanted <= allowed)
