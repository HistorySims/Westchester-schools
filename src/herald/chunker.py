"""Fixed-window chunker with sentence-snapping.

See PLAN.md §6: 400-word windows with 50-word overlap, snap boundaries to
nearest sentence end (or whitespace fallback). Article-boundary detection
and column reflow are deliberately out of scope for Phase 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_CHUNK_WORDS = 400
DEFAULT_OVERLAP_WORDS = 50
SENTENCE_SNAP_RADIUS_WORDS = 40

_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(?:\s|$)")


@dataclass(frozen=True)
class ChunkSpan:
    """A chunk by word offsets into the source text."""

    index: int
    word_start: int
    word_end: int
    content: str


def chunk_text(
    text: str,
    *,
    chunk_words: int = DEFAULT_CHUNK_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> list[ChunkSpan]:
    """Split text into overlapping word-windows, snapped to sentence ends.

    Returns chunks with ``index`` starting at 0 and ``word_start``/``word_end``
    in word units (half-open ``[start, end)``). Empty/whitespace-only input
    returns an empty list.
    """
    if chunk_words <= 0:
        raise ValueError("chunk_words must be positive")
    if overlap_words < 0 or overlap_words >= chunk_words:
        raise ValueError("overlap_words must be in [0, chunk_words)")

    words = text.split()
    n = len(words)
    if n == 0:
        return []

    stride = chunk_words - overlap_words
    spans: list[ChunkSpan] = []
    start = 0
    index = 0
    while start < n:
        ideal_end = min(start + chunk_words, n)
        end = _snap_end(words, start, ideal_end, n)
        content = " ".join(words[start:end])
        spans.append(
            ChunkSpan(index=index, word_start=start, word_end=end, content=content)
        )
        if end >= n:
            break
        start = max(end - overlap_words, start + stride)
        index += 1
    return spans


def _snap_end(words: list[str], start: int, ideal_end: int, n: int) -> int:
    """Adjust ``ideal_end`` to the nearest sentence boundary within
    ±SENTENCE_SNAP_RADIUS_WORDS, biased toward shorter chunks.
    Falls back to ``ideal_end`` if no sentence boundary is nearby.
    """
    if ideal_end >= n:
        return n
    radius = SENTENCE_SNAP_RADIUS_WORDS
    lo = max(start + 1, ideal_end - radius)
    hi = min(n, ideal_end + radius)
    best = -1
    best_dist = radius + 1
    for i in range(lo, hi):
        if _ends_sentence(words[i - 1]):
            dist = abs(i - ideal_end)
            if dist < best_dist:
                best = i
                best_dist = dist
    return best if best > start else ideal_end


def _ends_sentence(word: str) -> bool:
    if not word:
        return False
    return bool(_SENTENCE_END.match(word[-1] + " "))
