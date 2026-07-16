"""Structural chunker for BoardDocs agenda/minutes text.

Turns the flat text of a board agenda into chunks that follow the document's
own numbered outline, carrying an addressable section path + the human section
type (see docs/CHUNKING.md). Operates on *text* — PDF extraction is a separate
concern — so it is easy to test.

Strategy (adaptive, hybrid):
  * segment on the outline: top-level ``N.`` parts and lettered ``A.`` items;
  * a top-level section that fits in one chunk is emitted whole (merge);
  * a large section is split by its lettered items (one chunk per contract /
    action); a large narrative section with no letters is window-split;
  * anything still over the cap is sub-split with the inherited word-window
    chunker (``herald.chunker.chunk_text``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from herald.chunker import chunk_text as window_chunk

_TOP = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")   # 1. Call to Order
_LET = re.compile(r"^([A-Z])\.\s+(\S.*)$")      # A. Superintendent's Report

# Size thresholds in characters (~4 chars/token). Tunable.
MERGE_MIN = 250       # a segment smaller than this merges into its neighbor
TARGET = 1600         # a section at/under this is emitted as one chunk
MAX_CHARS = 3200      # a chunk over this is window-split

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_DATE_RE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE)
_MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(_MONTHS.split("|"))}


@dataclass
class Chunk:
    """A retrieval unit with its place in the agenda tree + doc metadata."""

    content: str
    section_path: str          # e.g. "P13.D"
    section_type: str          # e.g. "Consent Agenda - Business/Finance"
    heading: str
    order_index: int
    # doc-level, denormalized so chronology + district filter are 1-column ops
    district: str | None = None
    meeting_date: date | None = None
    doc_type: str | None = None
    source_url: str | None = None


@dataclass
class _Seg:
    level: int                 # -1 header, 0 top-level, 1 lettered
    path: list[str]
    section_type: str
    heading: str
    body: list[str] = field(default_factory=list)

    def text(self) -> str:
        head = "" if self.level < 0 else self.heading
        parts = [head, *self.body]
        return "\n".join(p for p in parts if p).strip()


def parse_meeting_date(text: str) -> date | None:
    """First 'Month DD, YYYY' in the text (the agenda header carries it)."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTH_NUM[m.group(1).lower()], int(m.group(2)))
    except (ValueError, KeyError):
        return None


def classify_doc_type(title: str) -> str:
    low = re.sub(r"[-_]+", " ", title.lower())
    if "minute" in low:
        return "minutes"
    if any(k in low for k in ("agenda", "business meeting", "work session", "boe meeting",
                              "board meeting", "special meeting", "regular meeting", "retreat")):
        return "agenda"
    if "policy" in low or "policies" in low or "regulation" in low:
        return "policy"
    if "handbook" in low:
        return "handbook"
    if "contract" in low or "agreement" in low or "mou" in low:
        return "contract"
    return "other"


def _segment(text: str) -> list[_Seg]:
    top_n = 0
    cur_type = "Header"
    cur = _Seg(level=-1, path=["header"], section_type="Header", heading="")
    segs: list[_Seg] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        mt = _TOP.match(ln)
        if mt and int(mt.group(1)) == top_n + 1:      # continues the top sequence
            segs.append(cur)
            top_n = int(mt.group(1))
            cur_type = mt.group(2).strip()
            cur = _Seg(level=0, path=[f"P{top_n}"], section_type=cur_type, heading=cur_type)
            continue
        ml = _LET.match(ln)
        if ml and top_n > 0:
            segs.append(cur)
            cur = _Seg(level=1, path=[f"P{top_n}", ml.group(1)],
                       section_type=cur_type, heading=ml.group(2).strip())
            continue
        cur.body.append(ln)                             # sub-items stay in the body
    segs.append(cur)
    return [s for s in segs if s.text()]


def _split_if_big(content: str) -> list[str]:
    if len(content) <= MAX_CHARS:
        return [content]
    return [span.content for span in window_chunk(content)] or [content]


def chunk_agenda_text(text: str, **doc_meta: object) -> list[Chunk]:
    """Chunk one agenda/minutes document's text. ``doc_meta`` (district,
    meeting_date, doc_type, source_url) is stamped onto every chunk."""
    segs = _segment(text)
    # group segments under their top-level section (header stands alone)
    groups: list[list[_Seg]] = []
    for s in segs:
        if s.level <= 0 or not groups:
            groups.append([s])
        else:
            groups[-1].append(s)

    out: list[Chunk] = []
    order = 0

    def emit(content: str, path: str, section_type: str, heading: str) -> None:
        nonlocal order
        pieces = _split_if_big(content)
        for i, piece in enumerate(pieces):
            # suffix split pieces so every chunk has a unique, ordered path
            p = path if len(pieces) == 1 else f"{path}#{i + 1}"
            out.append(Chunk(content=piece, section_path=p, section_type=section_type,
                             heading=heading, order_index=order, **doc_meta))  # type: ignore[arg-type]
            order += 1

    for group in groups:
        head = group[0]
        group_text = "\n\n".join(s.text() for s in group)
        letters = [s for s in group[1:] if s.level == 1]
        # Consent agendas split per lettered action (discrete items) regardless
        # of size; narrative sections stay whole unless oversize. No letters ->
        # always one chunk.
        enumerated = "consent agenda" in head.section_type.lower()
        if not letters or (not enumerated and len(group_text) <= TARGET):
            emit(group_text, ".".join(head.path), head.section_type,
                 head.heading or head.section_type)
            continue
        # large consent-style section -> one chunk per lettered item,
        # with the top-level intro folded into the first, and tiny items merged up.
        intro = head.text()
        pending = intro if intro and len(intro) < MERGE_MIN else ""
        if intro and not pending:
            emit(intro, ".".join(head.path), head.section_type, head.heading)
        for seg in letters:
            body = (pending + "\n\n" + seg.text()).strip() if pending else seg.text()
            pending = ""
            # Each lettered action is its own chunk (deep sub-fragments like the
            # personnel "Name:" lists are already folded into the letter body by
            # the segmenter, so they don't re-explode here).
            emit(body, ".".join(seg.path), seg.section_type, seg.heading)
    return out
