from herald.normalize import normalize_ocr, word_count


def test_empty_input_returns_empty():
    assert normalize_ocr("") == ""


def test_collapses_internal_whitespace():
    assert normalize_ocr("the   quick \t brown") == "the quick brown"


def test_preserves_single_linebreaks():
    assert normalize_ocr("line one\nline two") == "line one\nline two"


def test_collapses_runs_of_blank_lines():
    text = "first\n\n\n\n\nsecond"
    assert normalize_ocr(text) == "first\n\nsecond"


def test_strips_control_characters_but_keeps_tabs_and_newlines():
    raw = "hello\x00\x01world\nnext\tcol"
    assert normalize_ocr(raw) == "helloworld\nnext col"


def test_unicode_nfc_normalization():
    # "café" composed vs decomposed
    decomposed = "café"
    composed = "café"
    assert normalize_ocr(decomposed) == composed


def test_trims_leading_and_trailing_whitespace():
    assert normalize_ocr("   hello world   ") == "hello world"


def test_word_count_counts_split_tokens():
    assert word_count("the quick brown fox") == 4
    assert word_count("") == 0
    assert word_count("  spaced   out  words  ") == 3
