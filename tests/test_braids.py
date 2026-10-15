"""Tests for the beat layer — splitting agenda chunks into items.

The chunker emits one chunk per agenda *section*, and a section holds several
numbered items: Port Chester's 2026-02-26 agenda puts 8.1 through 8.5 in one
3,191-character chunk. One beat per chunk would compound five unrelated
initiatives into a single lifecycle, which docs/BRAIDS.md calls out as the
failure that corrupts every thread it touches.
"""

from __future__ import annotations

from herald.braids import (
    AgendaItem,
    items_with_chunks,
    split_agenda_items,
    state_for_item,
    state_from_type,
)
from herald.taxonomy import CANONICAL_STATES, normalize_state

# Shaped exactly like a BoardDocs section once the chunker has flattened it.
SECTION = (
    "Action Items "
    "Subject 8.1 Approval of the Services Agreement with Blooming Pathways "
    "Meeting Feb 26, 2026 - Board of Education Meeting - 6:00 p.m. "
    "Category 8. Action Items Type Action "
    "Recommended Action RESOLVED, that the Board of Education approves the agreement. "
    "File Attachments Services Agreement with Blooming Pathways 2025-26.pdf (104 KB) "
    "Subject 8.2 Approval of Budget Transfer(s) "
    "Meeting Feb 26, 2026 - Board of Education Meeting - 6:00 p.m. "
    "Category 8. Action Items Type Action (Consent) "
    "Recommended Action RESOLVED, that the Board approves the transfers below. "
    "File Attachments BT - 2-26-26.pdf (26 KB) "
    "Subject 8.3 Superintendent Report "
    "Meeting Feb 26, 2026 - Board of Education Meeting - 6:00 p.m. "
    "Category 8. Action Items Type Discussion "
    "The Superintendent will update the Board."
)


def test_one_chunk_yields_one_beat_per_item() -> None:
    items = split_agenda_items(SECTION)
    assert [i.number for i in items] == ["8.1", "8.2", "8.3"]
    assert items[0].subject == "Approval of the Services Agreement with Blooming Pathways"
    assert items[1].subject == "Approval of Budget Transfer(s)"
    # the subject must not run on into the meeting line
    assert "Meeting Feb" not in items[0].subject


def test_type_is_captured_so_state_is_not_always_other() -> None:
    """Regression: the meeting name used to swallow Category and Type.

    Both groups are optional, so the engine could satisfy them by matching
    nothing while `.+?` ate the rest of the preamble. Every item then came back
    with no Type, and every state silently became OTHER — 1,802 of 1,802 on the
    real corpus.
    """
    items = split_agenda_items(SECTION)
    assert [i.item_type for i in items] == ["Action", "Action (Consent)", "Discussion"]
    assert [state_from_type(i.item_type) for i in items] == [
        "PROPOSED", "PROPOSED", "DISCUSSED",
    ]
    assert items[1].is_consent and not items[0].is_consent


def test_attachments_attach_to_the_right_item_and_keep_their_spaces() -> None:
    """Regression: names were picking up the label they followed.

    A BoardDocs filename may contain spaces, so whitespace cannot delimit it,
    and a naive pattern started mid-phrase and produced
    "ile Attachments Services Agreement with...".
    """
    items = split_agenda_items(SECTION)
    assert items[0].attachments == ("Services Agreement with Blooming Pathways 2025-26.pdf",)
    assert items[1].attachments == ("BT - 2-26-26.pdf",)
    assert items[2].attachments == ()


def test_text_with_no_item_preamble_returns_nothing() -> None:
    """Minutes prose and bare section titles have no preamble to parse.

    An empty list is the signal to fall back to a model, not a defect — so it
    must not raise and must not invent an item.
    """
    assert split_agenda_items("") == []
    assert split_agenda_items("Public Comment") == []
    assert split_agenda_items(
        "Trustee Paine moved, seconded by Trustee Fletcher, that it be RESOLVED: "
        "That the Board of Education enters into Executive Session. CARRIED 6-0-0"
    ) == []


def test_item_without_a_number_still_becomes_a_beat() -> None:
    items = split_agenda_items(
        "Subject Policy Readings Meeting Jul 28, 2026 - BUSINESS MEETING AGENDA "
        "Category 8. Policy Readings Type Action Recommended Action That the Board reads."
    )
    assert len(items) == 1
    assert items[0].number == ""
    assert items[0].subject == "Policy Readings"


class TestNormalizeState:
    def test_every_rule_lands_in_the_locked_vocabulary(self) -> None:
        for raw in ("carried 4-3", "motion failed", "tabled until June",
                    "referred back to the policy committee", "second reading",
                    "30 day public comment period", "withdrawn by the Superintendent",
                    "first reading", "presentation and discussion", "nonsense"):
            assert normalize_state(raw) in CANONICAL_STATES

    def test_an_outcome_beats_the_motion_that_produced_it(self) -> None:
        # One sentence describes one adopted thing; "moved ... and carried"
        # must not read as merely PROPOSED.
        assert normalize_state(
            "Trustee Paine moved, seconded by Trustee Fletcher, and it CARRIED 6-0-0"
        ) == "VOTED_ADOPTED"
        assert normalize_state("moved, seconded, motion failed 2-5") == "VOTED_FAILED"

    def test_district_phrasings_for_the_same_outcome(self) -> None:
        for raw in ("CARRIED 6-0-0", "carried 4-3", "unanimously approved",
                    "adopted as presented", "ratified"):
            assert normalize_state(raw) == "VOTED_ADOPTED"

    def test_unrecognized_prefers_other_to_a_guess(self) -> None:
        assert normalize_state("") == "OTHER"
        assert normalize_state(None) == "OTHER"
        assert normalize_state("qqq zzz") == "OTHER"

    def test_a_canonical_value_passes_through(self) -> None:
        assert normalize_state("voted_adopted") == "VOTED_ADOPTED"


def test_agenda_item_is_hashable_and_frozen() -> None:
    a = AgendaItem(number="1.1", subject="x", category="c", item_type="Action", body="b")
    assert hash(a)


PEEKSKILL = (
    "Open Meeting Meeting Jan 13, 2026 - BUSINESS MEETING/WORK SESSION "
    "Category 2. Proposed Executive Session Subject to Board Approval 6:00 p.m. "
    "Type (Note: The Board will enter into Executive Session.)"
)


def test_an_item_with_no_subject_label_is_still_found() -> None:
    """Peekskill writes items with no label, and puts "Subject" in its category.

    Anchoring on the literal word found nothing for 75% of its chunks. The
    meeting stamp is the invariant; the subject is read backwards from it.
    """
    items = split_agenda_items(PEEKSKILL)
    assert len(items) == 1
    assert items[0].subject == "Open Meeting"
    # "Subject to Board Approval" is part of the category, not a new item
    assert "Subject to Board Approval" in items[0].category
    # an unrecognized Type matches nothing rather than swallowing the body
    assert items[0].item_type == ""


def test_state_falls_back_to_the_text_when_type_is_absent() -> None:
    """Greenburgh leaves Type empty on 399 of 565 items.

    That is how the district uses BoardDocs, not a parse failure, and the
    state is sitting in the body in plain sight.
    """
    typed = AgendaItem(number="8.1", subject="x", category="", item_type="Action", body="")
    assert state_for_item(typed) == "PROPOSED"

    untyped = AgendaItem(
        number="", subject="Approve the agreement", category="", item_type="",
        body="BE IT RESOLVED that as recommended by the Superintendent the Board approves.",
    )
    # text wins only because Type was empty; "approves" reads as an adoption
    assert state_for_item(untyped) != "OTHER"

    silent = AgendaItem(number="", subject="qqq", category="", item_type="", body="zzz")
    assert state_for_item(silent) == "OTHER"


def test_items_are_split_per_document_and_mapped_back_to_chunks() -> None:
    """The chunker splits mid-item, so per-chunk splitting loses the body.

    Greenburgh's agenda ends one chunk on "3. Business Contract: Benetech,
    Inc." and starts the next with its resolution text. Across its 200
    agendas, splitting per chunk left 34% of items with any body and 37% with
    a resolvable state; per document those become 100% and 91%. Citations
    still have to name a chunk, so each item is matched back to the one
    holding its subject.
    """
    doc = (
        "Subject 2. Contract: GunHill Basketball Meeting Jul 31, 2023 - Special Meeting "
        "Category 13. Business Type Action BE IT RESOLVED that the Board approves the "
        "contract at a cost not to exceed $1,400.00. "
        "Subject 3. Business Contract: Benetech, Inc. Meeting Jul 31, 2023 - Special Meeting "
        "Category 13. Business Type Action BE IT RESOLVED that the Board approves the "
        "Benetech agreement for assistive technology services."
    )
    # the seam falls inside the Benetech item, exactly as the chunker places it
    seam = doc.index("Subject 3.") + len("Subject 3. Business Contract: Benetech, Inc. ")
    chunks = [("c1", doc[:seam]), ("c2", doc[seam:])]

    # Per chunk the item disappears outright: its title sits at the end of c1
    # with the meeting stamp that identifies it now in c2, so neither half
    # parses as an item at all.
    per_chunk = [i for _cid, text in chunks for i in split_agenda_items(text)]
    assert not [i for i in per_chunk if "Benetech" in i.subject]

    paired = items_with_chunks(doc, chunks)
    assert [i.number for i, _ in paired] == ["2", "3"]
    whole = next(i for i, _ in paired if "Benetech" in i.subject)
    assert "assistive technology" in whole.body
    assert dict((i.number, c) for i, c in paired)["3"] == "c1"   # cited where its title is


def test_an_item_no_chunk_claims_reports_none_rather_than_guessing() -> None:
    doc = ("Subject 1. Roof Replacement at King Street Meeting Jan 5, 2026 - Regular Meeting "
           "Category 8. Action Type Action BE IT RESOLVED that the Board approves it.")
    assert items_with_chunks(doc, [("c1", "something else entirely")])[0][1] is None
