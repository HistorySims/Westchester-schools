"""Tests for cross-district drift on compliance documents.

Synthetic embeddings throughout, so the rules that decide "the state changed
this" against "the district chose this" can be pinned without a corpus or an
API key. The numbers in the regression tests are the ones measured on the pilot
corpus of 30 safety plans (docs/DRIFT_PILOT.md).
"""

from __future__ import annotations

import numpy as np
import pytest

from herald.drift_schools import (
    SectionChange,
    changed_sections,
    classify_change,
    consensus_movement,
    sections,
)


def vec(*xs: float) -> np.ndarray:
    v = np.zeros(8, dtype=np.float32)
    for i, x in enumerate(xs):
        v[i] = x
    return v


class TestSections:
    def test_prose_splits_near_the_target_length(self) -> None:
        text = " ".join(f"Sentence number {i} says something about safety." for i in range(200))
        out = sections(text, target=1400)
        assert len(out) > 4
        assert all(300 < len(s) < 1700 for s in out)

    def test_an_unpunctuated_run_is_still_split(self) -> None:
        """Regression: a table-heavy plan has almost no terminal punctuation.

        Peekskill's 2020-21 plan produced 26 sections from 87,000 characters
        where its own 2021-22 produced 63, because one "sentence" ran for
        thousands of characters. Those sections compare against nobody else's,
        and pooling a few enormous vectors is not the centroid that pooling
        sixty ordinary ones gives.
        """
        roster = " ".join(f"Name{i} Role{i} Building{i} 914-555-{i:04d}" for i in range(400))
        assert "." not in roster
        out = sections(roster, target=1400)
        assert len(out) > 8
        assert max(len(s) for s in out) < 2400

    def test_running_heads_and_page_numbers_are_dropped(self) -> None:
        body = "The committee reviews the plan each July and reports to the Board. " * 12
        with_furniture = "\n".join(
            f"District-wide School Safety Plan {i}\nPage {i} of 40\n{body}" for i in range(3)
        )
        out = sections(with_furniture)
        assert out
        assert not any("Page 1 of 40" in s for s in out)

    def test_nothing_in_nothing_out(self) -> None:
        assert sections("") == []
        assert sections("   \n  ") == []
        # A heading alone is not a passage.
        assert sections("SECTION 4: EVACUATION") == []


class TestChangedSections:
    def test_untouched_template_ranks_below_a_rewrite(self) -> None:
        previous = np.array([vec(1, 0), vec(0, 1)])
        # first section unchanged, second rewritten into a new direction
        current = np.array([vec(1, 0.01), vec(0.3, 0.3, 0.9)])
        out = changed_sections(current, previous, ["template", "rewritten"])
        assert out[0].text == "rewritten"
        assert out[0].distance > out[1].distance
        assert out[1].distance < 0.01

    def test_a_peer_filing_the_same_passage_marks_it_shared(self) -> None:
        previous = np.array([vec(1, 0)])
        current = np.array([vec(0, 1)])
        peers = np.array([vec(0, 1), vec(1, 0)])
        out = changed_sections(current, previous, ["x"], peers=peers)
        assert out[0].is_shared

    def test_with_no_peers_nothing_is_called_shared(self) -> None:
        """A district with no comparable filing that year must not read as local.

        Absent peers, best_peer is 0 and every change would classify as a local
        decision — the one inference the method is not entitled to make.
        """
        out = changed_sections(np.array([vec(0, 1)]), np.array([vec(1, 0)]), ["x"])
        assert out[0].best_peer == 0.0
        assert not out[0].is_shared

    def test_an_absent_previous_filing_yields_nothing(self) -> None:
        empty = np.zeros((0, 8), dtype=np.float32)
        assert changed_sections(np.array([vec(1, 0)]), empty, ["x"]) == []
        assert changed_sections(empty, np.array([vec(1, 0)]), []) == []

    def test_texts_must_line_up_with_vectors(self) -> None:
        with pytest.raises(ValueError):
            changed_sections(np.array([vec(1, 0)]), np.array([vec(1, 0)]), ["a", "b"])

    def test_unnormalized_vectors_are_handled(self) -> None:
        out = changed_sections(
            np.array([vec(7, 0)]), np.array([vec(0.001, 0)]), ["x"]
        )
        assert out[0].distance < 1e-5


class TestClassifyChange:
    def _change(self, distance: float, best_peer: float) -> SectionChange:
        return SectionChange(index=0, distance=distance, best_peer=best_peer, text="x")

    def test_the_three_outcomes(self) -> None:
        assert classify_change(self._change(0.01, 0.0)) == "unchanged"
        assert classify_change(self._change(0.23, 0.95)) == "mandate"
        assert classify_change(self._change(0.28, 0.72)) == "local"

    def test_the_pilot_measurements_land_where_they_should(self) -> None:
        """The numbers that came off the 30-plan corpus, as a guard on the gates.

        The amended drill language that three districts adopted in 2024-25
        matched between peers at 0.90-0.97; the police MOUs and Elmsford's own
        reunification design sat at 0.67-0.79. A threshold change that stops
        separating these two groups has broken the method.
        """
        for distance, peer in ((0.229, 0.97), (0.261, 0.94), (0.184, 0.90)):
            assert classify_change(self._change(distance, peer)) == "mandate"
        for distance, peer in ((0.281, 0.72), (0.303, 0.72), (0.241, 0.76)):
            assert classify_change(self._change(distance, peer)) == "local"

    def test_a_big_move_nobody_shares_is_local_not_mandate(self) -> None:
        assert classify_change(self._change(0.39, 0.58)) == "local"


class TestConsensusMovement:
    def test_a_group_that_moved_together_registers(self) -> None:
        a = {"d1": np.array([vec(1, 0)]), "d2": np.array([vec(1, 0.1)])}
        b = {"d1": np.array([vec(0, 1)]), "d2": np.array([vec(0.1, 1)])}
        assert consensus_movement(a, b) > 0.5

    def test_a_group_that_stood_still_registers_nothing(self) -> None:
        a = {"d1": np.array([vec(1, 0)]), "d2": np.array([vec(1, 0.1)])}
        assert consensus_movement(a, dict(a)) < 1e-5

    def test_fewer_than_two_shared_districts_is_not_a_consensus(self) -> None:
        a = {"d1": np.array([vec(1, 0)])}
        b = {"d1": np.array([vec(0, 1)]), "d2": np.array([vec(0, 1)])}
        # d2 has no 'a' filing, so one district remains: no group to speak of
        assert consensus_movement(a, b) == 0.0
        assert consensus_movement({}, {}) == 0.0
