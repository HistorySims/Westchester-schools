"""Light OCR cleanup. No de-hyphenation, no column reflow — see PLAN.md §4.1."""

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"[ \t\f\v]+")
_BLANK_LINE_RUN = re.compile(r"\n\s*\n\s*(\n\s*)+")
_CONTROL_CHARS = "".join(chr(c) for c in range(32) if chr(c) not in "\n\t")
_CONTROL_RE = re.compile(f"[{re.escape(_CONTROL_CHARS)}]")


def normalize_ocr(text: str) -> str:
    """Normalize raw LOC OCR text.

    Steps: Unicode NFC, strip control chars (preserve \\n and \\t), collapse
    runs of spaces/tabs, collapse 3+ blank lines to a paragraph break.
    Line breaks are preserved — they carry weak column-boundary signal that
    later phases may exploit.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RUN.sub(" ", text)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(text.split())
