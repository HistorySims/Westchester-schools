from herald.chunker import ChunkSpan, chunk_text


def test_empty_text_returns_empty_list():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_returns_single_chunk():
    text = "one two three four five."
    spans = chunk_text(text)
    assert len(spans) == 1
    assert spans[0].index == 0
    assert spans[0].word_start == 0
    assert spans[0].word_end == 5
    assert spans[0].content == text


def test_chunks_overlap_by_overlap_words():
    # 100 unique words; window=10, overlap=2 -> stride 8
    words = [f"w{i}" for i in range(100)]
    text = " ".join(words)
    spans = chunk_text(text, chunk_words=10, overlap_words=2)
    assert spans[0].word_start == 0
    assert spans[0].word_end == 10
    # second chunk starts overlap_words before the previous end
    assert spans[1].word_start == 8
    assert spans[1].word_end == 18
    # overlap content shared across the boundary
    last_two_of_first = spans[0].content.split()[-2:]
    first_two_of_second = spans[1].content.split()[:2]
    assert last_two_of_first == first_two_of_second


def test_no_overlap_means_disjoint_chunks():
    words = [f"w{i}" for i in range(30)]
    text = " ".join(words)
    spans = chunk_text(text, chunk_words=10, overlap_words=0)
    assert [(s.word_start, s.word_end) for s in spans] == [(0, 10), (10, 20), (20, 30)]


def test_last_chunk_does_not_overshoot():
    words = [f"w{i}" for i in range(25)]
    text = " ".join(words)
    spans = chunk_text(text, chunk_words=10, overlap_words=2)
    assert spans[-1].word_end == 25
    assert all(s.word_end <= 25 for s in spans)


def test_snaps_to_sentence_boundary_inside_radius():
    # Words 0..12; "eta." at index 6 ends a sentence. Ideal window ends at 10,
    # but snap pulls boundary back to 7 (half-open: chunk contains words 0..6).
    parts = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta.", "theta",
             "iota", "kappa", "lambda", "mu", "nu"]
    text = " ".join(parts)
    spans = chunk_text(text, chunk_words=10, overlap_words=2)
    assert spans[0].word_end == 7
    assert spans[0].content.endswith("eta.")


def test_validates_chunk_and_overlap_sizes():
    import pytest
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_words=0)
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_words=5, overlap_words=5)
    with pytest.raises(ValueError):
        chunk_text("hi", chunk_words=5, overlap_words=-1)


def test_spans_are_indexed_sequentially():
    words = [f"w{i}" for i in range(50)]
    spans = chunk_text(" ".join(words), chunk_words=10, overlap_words=2)
    assert [s.index for s in spans] == list(range(len(spans)))


def test_chunk_span_is_frozen():
    import dataclasses
    span = ChunkSpan(index=0, word_start=0, word_end=1, content="x")
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        span.index = 1  # type: ignore[misc]
