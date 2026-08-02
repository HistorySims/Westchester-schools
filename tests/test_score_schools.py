"""Tests for the schools quality / boilerplate scorer."""

from __future__ import annotations

from herald.score_schools import ScoreResult, procedural_families, score_chunk

CLEAN = (
    "The board of education will review the district policy on student "
    "attendance and update the handbook for the next school year to reflect "
    "the new state requirements for excused and unexcused absences."
)

PROCEDURAL = (
    "Roll call. Members present: Smith, Jones, and Lee. Motion by Smith, "
    "seconded by Jones, to approve the minutes of the previous meeting. "
    "All in favor. Carried unanimously. The meeting was adjourned."
)

GIBBERISH = "xqz vbn mnp qwrt zxcv plok jhgf tred wsxa lkjh gfds"


def test_clean_content_stays_active():
    r = score_chunk(CLEAN)
    assert r.status == "active"
    assert r.quality_score > 0.3


def test_procedural_boilerplate_quarantined():
    assert procedural_families(PROCEDURAL) >= 2
    r = score_chunk(PROCEDURAL)
    assert r.status == "quarantined" and r.reason == "procedural"


def test_illegible_ocr_quarantined():
    r = score_chunk(GIBBERISH)
    assert r.status == "quarantined" and r.reason == "ocr_illegible"


def test_too_short_quarantined():
    r = score_chunk("Board of Education Regular Meeting")  # 5 words
    assert r.status == "quarantined" and r.reason == "too_short"


def test_long_substantive_motion_survives():
    # a real budget motion (one procedural family) inside a long passage stays
    text = (
        "Upon a motion duly made, the board adopted the 2026-2027 budget of "
        "$84.2 million, which increases the tax levy by 2.1 percent, funds two "
        "additional special education teachers, expands the districtwide mental "
        "health program, and allocates capital reserves toward the replacement "
        "of the middle school roof and the upgrade of the athletic fields, as "
        "detailed in the superintendent's budget presentation to the community."
    )
    r = score_chunk(text)
    assert r.status == "active"  # only one procedural family, and it's long


def test_score_result_shape():
    r = score_chunk(CLEAN)
    assert isinstance(r, ScoreResult)
    assert 0.0 <= r.quality_score <= 1.0
