"""Tests for the Panopto probe's pure parts.

The probe itself talks to a live host from an Actions runner (the egress proxy
denies Panopto here, the same reason `herald-scrape probe` exists for
BoardDocs). What can be tested without a network is the parsing — and the
caption cleanup, which is the part that decides whether a transcript is
usable or is mostly duplicated sentences.
"""

from __future__ import annotations

import json

from herald.scrape.panopto import (
    caption_url,
    looks_like_srt,
    parse_sessions,
    sessions_query,
    srt_to_text,
)

SRT = """1
00:00:01,000 --> 00:00:04,000
Motion by Trustee Rivera to approve the consent agenda.

2
00:00:04,000 --> 00:00:07,500
Motion by Trustee Rivera to approve the consent agenda.

3
00:00:07,500 --> 00:00:11,000
<i>Seconded by Trustee Chen.</i>

4
00:00:11,000 --> 00:00:14,000
All in favor?  Motion carries seven to zero.
"""


def test_srt_becomes_prose_without_cue_numbers_or_timecodes():
    text = srt_to_text(SRT)
    assert "00:00:01" not in text and "-->" not in text
    assert not any(line.strip().isdigit() for line in text.splitlines())
    assert "Motion by Trustee Rivera" in text
    assert "Seconded by Trustee Chen." in text      # markup stripped
    assert "Motion carries seven to zero." in text


def test_a_caption_held_across_cues_is_not_repeated():
    # Panopto re-emits a caption while it stays on screen. Left in, a
    # transcript is mostly duplicated sentences and every chunk of it looks
    # like every other chunk to an embedder.
    text = srt_to_text(SRT)
    assert text.count("Motion by Trustee Rivera to approve the consent agenda.") == 1


def test_srt_detection_separates_captions_from_a_refusal():
    # A request against a restricted folder answers 200 with an empty body, so
    # the status code alone cannot tell success from refusal.
    assert looks_like_srt(SRT)
    assert not looks_like_srt("")
    assert not looks_like_srt("<html><body>Please sign in</body></html>")


def test_empty_and_malformed_srt_do_not_raise():
    assert srt_to_text("") == ""
    assert srt_to_text("no timecodes here") == "no timecodes here"


def test_parse_sessions_tolerates_the_shapes_panopto_returns():
    rows = [{"DeliveryID": "abc-123", "SessionName": "Board of Education Meeting",
             "StartTime": "2026-05-28T23:00:00Z", "FolderName": "Board Meetings"}]
    expected = [("abc-123", "Board of Education Meeting")]

    for payload in (
        {"d": {"Results": rows}},          # wrapped twice
        {"Results": rows},                  # wrapped once
        rows,                               # bare list
        json.dumps({"d": {"Results": rows}}),  # as a JSON string
    ):
        got = parse_sessions(payload)
        assert [(s.id, s.name) for s in got] == expected, payload

    s = parse_sessions(rows)[0]
    assert s.date == "2026-05-28T23:00:00Z" and s.folder == "Board Meetings"


def test_parse_sessions_returns_empty_rather_than_raising():
    # A probe that raises tells us less than one that reports nothing found.
    for payload in ({}, [], {"Results": None}, "null", [{"no": "id"}]):
        assert parse_sessions(payload) == []


def test_caption_url_and_query_shape():
    assert caption_url("h.panopto.com", "S1") == (
        "https://h.panopto.com/Panopto/Pages/Transcription/GenerateSRT.ashx?id=S1&language=1"
    )
    q = sessions_query("FOLDER1")["queryParameters"]
    assert q["folderID"] == "FOLDER1" and q["maxResults"] == 50
    assert sessions_query()["queryParameters"]["folderID"] is None
