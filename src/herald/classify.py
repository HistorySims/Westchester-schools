"""Heuristic content-type classification for newspaper chunks.

Categories:
  0 = content (news, editorials, correspondence)
  1 = ad (advertisements, classifieds)
  2 = legal (legal notices, court announcements)
  3 = bad_ocr (garbled / unintelligible text)

Quality scoring (separate from category): a continuous read of OCR
quality used by scripts/score_chunk_quality.py to populate the
chunks.status / .quality_score / .quality_subscores columns.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

CONTENT = 0
AD = 1
LEGAL = 2
BAD_OCR = 3

_PRICE_RE = re.compile(
    r"\$\s?\d|cents?\b|per\s+annum|dollars?\b|shillings?\b", re.IGNORECASE
)
_AD_KEYWORDS_RE = re.compile(
    r"\bFOR\s+SALE\b|\bWANTED\b|\bTO\s+LET\b|\bAUCTION\b|\bREWARD\b"
    r"|\bAPPLY\s+(TO|AT)\b|\bPRICE\b|\bDISSO(LU|LV)TION\b",
    re.IGNORECASE,
)
_LEGAL_RE = re.compile(
    r"NOTICE\s+IS\s+HEREBY\s+GIVEN"
    r"|IN\s+PURSUANCE\s+OF"
    r"|BY\s+ORDER\s+OF"
    r"|SUPREME\s+COURT"
    r"|CHANCERY"
    r"|CHANCELLOR"
    r"|\bMORTGAGE\b"
    r"|\bFORECLOSURE\b"
    r"|\bPURSUANT\s+TO\s+STATUTE\b"
    r"|\bSURROGATE\b",
    re.IGNORECASE,
)

_WORDLIST: set[str] | None = None
_WORDLIST_PATH = Path(__file__).parent / "wordlist.txt"


def _load_wordlist() -> set[str]:
    global _WORDLIST
    if _WORDLIST is not None:
        return _WORDLIST
    if _WORDLIST_PATH.exists():
        _WORDLIST = {
            w.strip().lower()
            for w in _WORDLIST_PATH.read_text().splitlines()
            if w.strip()
        }
    else:
        _WORDLIST = set()
    return _WORDLIST


def _is_bad_ocr(content: str) -> bool:
    if not content.strip():
        return True
    words = content.split()
    if len(words) < 5:
        return False
    alpha_chars = sum(1 for c in content if c.isalpha())
    total_chars = len(content.replace(" ", ""))
    if total_chars == 0:
        return True
    if alpha_chars / total_chars < 0.6:
        return True
    avg_word_len = sum(len(w) for w in words) / len(words)
    if avg_word_len < 2 or avg_word_len > 15:
        return True
    wordlist = _load_wordlist()
    if wordlist:
        known = sum(1 for w in words if w.lower().strip(".,;:!?\"'()-") in wordlist)
        if known / len(words) < 0.3:
            return True
    return False


def _is_legal(content: str) -> bool:
    matches = len(_LEGAL_RE.findall(content))
    return matches >= 2


def _is_ad(content: str) -> bool:
    words = content.split()
    if len(words) < 80 and _PRICE_RE.search(content):
        return True
    if _AD_KEYWORDS_RE.search(content):
        return True
    return False


def classify_chunk(content: str) -> int:
    if _is_bad_ocr(content):
        return BAD_OCR
    if _is_legal(content):
        return LEGAL
    if _is_ad(content):
        return AD
    return CONTENT


LABELS = {CONTENT: "content", AD: "ad", LEGAL: "legal", BAD_OCR: "bad_ocr"}


# ---- continuous quality scoring -----------------------------------

@dataclass(frozen=True)
class QualityScores:
    """Continuous OCR quality sub-scores. Higher dict_word_ratio is better."""

    non_alpha_ratio: float    # fraction of non-alphabetic chars (excl. spaces); 1.0 = no letters
    avg_word_len: float       # mean characters per whitespace-split token
    dict_word_ratio: float    # fraction of tokens recognised by the bundled wordlist; 1.0 = perfect
    word_count: int           # raw token count, exposed for downstream filtering

    def composite(self) -> float:
        """Single scalar in [0, 1] suitable for chunks.quality_score.

        Mostly dict_word_ratio with structural penalties so a chunk
        full of single-letter junk doesn't score the same as a chunk
        full of names.
        """
        score = self.dict_word_ratio
        if self.avg_word_len < 2.0 or self.avg_word_len > 16.0:
            score *= 0.5
        if self.non_alpha_ratio > 0.5:
            score *= 0.5
        return max(0.0, min(1.0, score))

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


# Quarantine thresholds. dict_word_ratio is the primary signal.
#
# What `quarantined` actually means: the OCR is unreadable enough that
# embedding / FTS retrieval on it returns noise. It does NOT mean the
# chunk is lost — every quarantined chunk has a LoC page link, and a
# historian can click through to read the original image. The Phase A
# recovery targeting system surfaces them for that purpose. So
# "quarantine" is about hiding garbage from machine-driven RAG, not
# about discarding the source.
#
# The bar used to require BOTH a low dict ratio AND a structural
# break, which was too lenient — most damaged OCR has decent word
# lengths and reasonable alpha-ratio, so it failed the structural
# check and stayed active. The new bar: anything below
# QUARANTINE_DICT_RATIO is quarantined regardless of structure; the
# structural check now adds a second-stage gate up to
# QUARANTINE_DICT_RATIO_SOFT.
QUARANTINE_DICT_RATIO = 0.18
QUARANTINE_DICT_RATIO_SOFT = 0.28
# Above the quarantine bar but still marginal: a sizeable fraction of
# the text is unreadable, which makes the embedding noisy and may
# have put the chunk in the wrong cluster. These are flagged as
# REASSIGNMENT candidates — a human or a later pass might want to
# review where they actually belong, not necessarily re-OCR them.
REASSIGNMENT_CANDIDATE_DICT_RATIO = 0.40


def compute_quality_scores(content: str) -> QualityScores:
    """Continuous quality sub-scores for one chunk."""
    if not content or not content.strip():
        return QualityScores(
            non_alpha_ratio=1.0, avg_word_len=0.0, dict_word_ratio=0.0, word_count=0,
        )

    words = content.split()
    word_count = len(words)
    if word_count == 0:
        return QualityScores(
            non_alpha_ratio=1.0, avg_word_len=0.0, dict_word_ratio=0.0, word_count=0,
        )

    alpha_chars = sum(1 for ch in content if ch.isalpha())
    no_space = content.replace(" ", "").replace("\t", "").replace("\n", "")
    non_alpha_ratio = (
        1.0 - (alpha_chars / len(no_space)) if len(no_space) > 0 else 1.0
    )

    avg_word_len = sum(len(w) for w in words) / word_count

    wordlist = _load_wordlist()
    if wordlist:
        known = sum(
            1 for w in words if w.lower().strip(".,;:!?\"'()-") in wordlist
        )
        dict_word_ratio = known / word_count
    else:
        dict_word_ratio = 0.0

    return QualityScores(
        non_alpha_ratio=non_alpha_ratio,
        avg_word_len=avg_word_len,
        dict_word_ratio=dict_word_ratio,
        word_count=word_count,
    )


def classify_quality(scores: QualityScores) -> tuple[str, str | None]:
    """Map continuous scores → (status, reason).

    Two-stage bar so unreadable-but-structurally-normal chunks don't
    leak into RAG:
      - dict_ratio < QUARANTINE_DICT_RATIO   → quarantine (unconditional)
      - dict_ratio < QUARANTINE_DICT_RATIO_SOFT AND structurally weak
                                             → quarantine
      - dict_ratio < REASSIGNMENT_CANDIDATE_DICT_RATIO
                                             → 'reassignment_candidate' (active)
      - otherwise                            → active

    Note on reasons: 'quarantined' chunks aren't lost — every one has
    a LoC page link and the historian can click through to read the
    image. The Phase A recovery targeting system surfaces them for
    that purpose. 'reassignment_candidate' chunks have enough
    intelligible text to be useful but enough OCR noise that their
    embedding may have placed them in the wrong cluster; the name
    reflects the review they may benefit from, not OCR recovery.

    Returns (status, reason). reason may be None for clean chunks.
    """
    if scores.word_count < 3:
        return ("quarantined", "too_short")

    structurally_weak = (
        scores.avg_word_len < 2.0
        or scores.avg_word_len > 16.0
        or scores.non_alpha_ratio > 0.5
    )

    # Hard floor: below this we can't read the chunk no matter what.
    # 'ocr_illegible' replaces the old 'garbage_ocr' — these chunks
    # aren't garbage to the historian (the LoC image is intact); they
    # just don't yield enough machine-readable text for RAG.
    if scores.dict_word_ratio < QUARANTINE_DICT_RATIO:
        return ("quarantined", "ocr_illegible")
    # Soft floor: weakly readable, but also structurally suspect.
    if scores.dict_word_ratio < QUARANTINE_DICT_RATIO_SOFT and structurally_weak:
        return ("quarantined", "ocr_illegible")
    # Active but enough noise that cluster placement may be wrong.
    if scores.dict_word_ratio < REASSIGNMENT_CANDIDATE_DICT_RATIO:
        return ("active", "reassignment_candidate")
    return ("active", None)
