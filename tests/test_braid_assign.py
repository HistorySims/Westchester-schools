"""Tests for assigning beats to threads (docs/BRAIDS.md §2).

Pure logic: the embeddings are synthetic, so the rules that decide whether a
beat joins a thread or starts one can be pinned without a corpus or a key.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from herald.braid_assign import (
    Beat,
    Thread,
    anchorable_phrases,
    assign,
    audit,
    drift,
    extract_identifiers,
    extract_key_phrases,
    phrase_document_frequency,
    run_assignment,
    update_centroid,
)


def vec(*xs: float) -> np.ndarray:
    v = np.zeros(8, dtype=np.float32)
    for i, x in enumerate(xs):
        v[i] = x
    return v


def beat(key: str, day: int, subject: str, **kw) -> Beat:
    return Beat(
        key=key, district="port-chester-rye",
        meeting_date=date(2026, 1, 1) + timedelta(days=day),
        subject=subject, state=kw.pop("state", "PROPOSED"), **kw,
    )


class TestIdentifiers:
    def test_finds_the_kinds_worth_anchoring_on(self) -> None:
        assert "5030" in extract_identifiers("Second reading of Policy 5030")
        assert "24-017" in extract_identifiers("Award of Bid #24-017 for paving")

    def test_a_bare_four_digit_number_is_not_an_identifier(self) -> None:
        """Years, dollar figures and room numbers all look like policy numbers.

        A false anchor is a false merge, which is the worst outcome available,
        so a pattern has to earn its place.
        """
        assert extract_identifiers("Adoption of the 2026 calendar") == ()
        assert extract_identifiers("Transfer of $4,500 to the reserve") == ()


class TestKeyPhrases:
    def test_a_shared_name_survives_different_surrounding_words(self) -> None:
        """This is the whole point: the storyline's steps share only a name."""
        a = set(extract_key_phrases(
            "SEQRA Type II Classification for Proposed Capital Improvements - "
            "Security Vestibules and Lighting"))
        b = set(extract_key_phrases("Resolution Rejecting Bids for Security Vestibules"))
        c = set(extract_key_phrases(
            "Rejecting Non-Responsive Bid and Awarding Contract for "
            "Security Vestibules MS & King Street"))
        assert "security vestibules" in a & b & c

    def test_agenda_furniture_yields_no_anchor(self) -> None:
        assert extract_key_phrases("Pledge of Allegiance") == ()
        assert extract_key_phrases("Committee Reports") == ()


class TestRarityGate:
    def test_a_category_phrase_is_refused_and_a_name_is_kept(self) -> None:
        """"Competitive bid" is a real phrase used for every procurement.

        The stoplist cannot catch it — it is not furniture the way "pledge of
        allegiance" is — but document frequency separates them cleanly.
        Anchoring on it merged a copier lease, a transportation contract, a
        food-service RFP and the security vestibules into one 20-beat thread.
        """
        beats = [beat(f"b{i}", i, "Competitive Bid for Services",
                      phrases=("competitive bid",)) for i in range(20)]
        rare = beat("x", 99, "Security Vestibules at JFK",
                    phrases=("competitive bid", "security vestibules"))
        beats.append(rare)
        df = phrase_document_frequency(beats)
        keep = anchorable_phrases(rare, df, max_df=0.1)
        assert keep == {"security vestibules"}


class TestAssign:
    def _thread(self, **kw) -> Thread:
        t = Thread(id="t1", district="port-chester-rye", title="x",
                   first_seen=date(2026, 1, 1), last_seen=date(2026, 1, 1), **kw)
        update_centroid(t, vec(1, 0))
        return t

    def test_an_identifier_wins_over_everything(self) -> None:
        t = self._thread(identifiers={"5030"})
        # pointing the other way in embedding space, and it still attaches
        a = assign(beat("b", 5, "x", identifiers=("5030",)), vec(0, 1), [t])
        assert a.thread_id == "t1" and a.via == "anchor"

    def test_below_the_floor_starts_a_new_thread(self) -> None:
        """Spawn conservatively: a spurious thread is cheap, a false merge is not."""
        a = assign(beat("b", 5, "x"), vec(0, 1), [self._thread()], floor=0.9)
        assert a.is_new and a.via == "new"

    def test_a_shared_rare_phrase_attaches_below_the_similarity_floor(self) -> None:
        t = self._thread(phrases={"security vestibules"})
        b = beat("b", 5, "Rejecting Bids for Security Vestibules",
                 phrases=("security vestibules",))
        # similar enough to corroborate, nowhere near the 0.92 similarity floor
        a = assign(b, vec(0.8, 0.6), [t], floor=0.92,
                   rare_phrases={"security vestibules"})
        assert a.thread_id == "t1" and a.via == "anchor"

    def test_a_shared_phrase_alone_is_not_enough(self) -> None:
        """The phrase is evidence, not proof — similarity still has to agree."""
        t = self._thread(phrases={"security vestibules"})
        b = beat("b", 5, "unrelated", phrases=("security vestibules",))
        a = assign(b, vec(0, 1), [t], floor=0.92, phrase_floor=0.72,
                   rare_phrases={"security vestibules"})
        assert a.is_new

    def test_threads_in_another_district_are_never_candidates(self) -> None:
        t = self._thread()
        t.district = "peekskill"
        assert assign(beat("b", 1, "x"), vec(1, 0), [t]).is_new

    def test_a_stale_thread_leaves_the_similarity_window(self) -> None:
        t = self._thread()
        far = beat("b", 2000, "x")
        assert assign(far, vec(1, 0), [t]).is_new
        # but an identifier ignores the window entirely — that is how a
        # reintroduced issue is caught years later
        t.identifiers = {"5030"}
        assert assign(
            beat("b", 2000, "x", identifiers=("5030",)), vec(0, 1), [t]
        ).via == "anchor"


def test_run_assignment_builds_forward_in_time() -> None:
    """Out of order, a later beat could found the thread its predecessor
    then fails to join."""
    beats = [beat("late", 30, "Security Vestibules phase two",
                  phrases=("security vestibules",)),
             beat("early", 1, "Security Vestibules study",
                  phrases=("security vestibules",))]
    vecs = [vec(1, 0.05), vec(1, 0)]
    threads, _ = run_assignment(beats, vecs, floor=0.99)
    assert len(threads) == 1
    assert threads[0].first_seen == date(2026, 1, 2)   # the early beat founded it
    assert threads[0].beat_count == 2


def test_drift_rises_as_a_thread_moves_away_from_where_it_started() -> None:
    t = Thread(id="t", district="d", title="x",
               first_seen=date(2026, 1, 1), last_seen=date(2026, 1, 1))
    update_centroid(t, vec(1, 0))
    assert drift(t) == 0.0
    for _ in range(12):
        update_centroid(t, vec(0, 1))
    assert drift(t) > 0.5


def test_audit_reports_the_zombie_rate() -> None:
    """All-singletons looks like success — many threads — and is the opposite."""
    beats = [beat(f"b{i}", i * 10, f"unrelated subject {i}") for i in range(5)]
    vecs = [vec(*([0] * i + [1])) for i in range(5)]
    threads, asg = run_assignment(beats, vecs, floor=0.99)
    flags = audit(threads, asg)
    assert flags.threads == 5
    assert flags.zombie_rate == 1.0
