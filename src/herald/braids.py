"""Narrative threads over board documents — the beat layer (docs/BRAIDS.md).

A *beat* is one discrete event in an issue's life: an item proposed, discussed,
referred, revised, adopted. Threads are chains of beats; this module produces
the beats.

The design assumed a model call per chunk. For BoardDocs agendas most of that
is unnecessary, because the source is already structured — every item is
rendered as a fixed ``Subject / Meeting / Category / Type`` preamble followed by
its body. Splitting a chunk into items and reading their subjects is therefore
deterministic, free, and exactly reproducible, and a model is only needed where
the text is genuinely prose: minutes, and inferring state from a narrative
sentence.

Two facts from the probe (docs/BRAIDS_PROBE.md) shape this:

* **A chunk is a section, not an item.** Port Chester's 2026-02-26 agenda puts
  items 8.1 through 8.5 in one 3,191-character chunk. One beat per chunk would
  compound five unrelated initiatives into one lifecycle, which is the failure
  BRAIDS.md calls out as corrupting every thread it touches.
* **Assignment should match on the subject, not the chunk.** Embedding whole
  chunks puts nearest-neighbour topic accuracy at 74.7% and precision@5 at
  66.6%; embedding just the item's subject line gives 95.9% and 95.2%. The
  scaffolding the preamble repeats is most of what the vector sees.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from herald.taxonomy import normalize_state

#: BoardDocs renders an item as:
#:
#:     Subject 8.1 Approval of the Services Agreement with Blooming Pathways
#:     Meeting Feb 26, 2026 - Board of Education Meeting - 6:00 p.m.
#:     Category 8. Action Items
#:     Type Action (Consent)
#:     Recommended Action RESOLVED, that ...
#:     File Attachments foo.pdf (12 KB)
#:
#: flattened to one line by the chunker. The preamble is rigid; the body is not.
#: The one thing every BoardDocs item has is a meeting stamp. The "Subject"
#: label does not qualify: Peekskill renders items as "Open Meeting Meeting
#: Jan 13, 2026 - ..." with no label at all, and puts the *word* Subject inside
#: its category ("Proposed Executive Session Subject to Board Approval"). So
#: anchor on the stamp and read the subject backwards from it.
_MEETING_STAMP = re.compile(r"Meeting\s+\w{3}\s+\d{1,2},\s+\d{4}\s*-\s*", re.I)

#: The labels BoardDocs uses in an item's Type field.
_TYPE_TOKEN = (
    r"(?:Action \(Consent\)|Action|Discussion|Information|Info|"
    r"Procedural|Report|Presentation)"
)

#: What follows the stamp, in order, each field running to the next label.
_TAIL = re.compile(
    r"(?P<meeting>.*?)(?=\s+Category\s|\s+Type\s|$)"
    r"(?:\s+Category\s+(?P<category>.*?)(?=\s+Type\s|$))?"
    # Type is a short label, and Mount Vernon combines them ("Discussion,
    # Information, Presentation"). Matching free text instead ran to the end of
    # the chunk whenever an item had no Recommended Action to stop at. An
    # unrecognized label matches nothing here and becomes OTHER downstream,
    # which is the right answer for Peekskill's "(Note: the Board will...)".
    r"(?:\s+Type\s+(?P<type>" + _TYPE_TOKEN + r"(?:\s*,\s*" + _TYPE_TOKEN + r")*))?",
    re.I | re.S,
)

#: A subject may be labelled ("Subject 8.1 Approval of ...") or bare ("Open
#: Meeting"). Strip the label and number when they are there.
#: Port Chester numbers items "8.1", Greenburgh "3." — the trailing period
#: has to be optional, or the number is never stripped and rides along in
#: the subject ("3. Business Contract: Benetech, Inc"), where it pollutes
#: both the embedding and the phrase anchors.
_SUBJECT_LABEL = re.compile(
    r"^.*?\bSubject\s+(?:(?P<num>\d+(?:\.\d+)*)\.?\s+)?", re.I | re.S
)

#: How far back from a stamp a bare subject may reach. Long enough for a real
#: title, short enough that an unlabelled item does not drag in the whole
#: preceding paragraph.
_SUBJECT_LOOKBACK = 140

#: A BoardDocs attachment renders as "<name>.pdf (123 KB)", and the name may
#: contain spaces ("Services Agreement with Blooming Pathways 2025-26.pdf"), so
#: whitespace cannot delimit it. Scanning forward from the "File Attachments"
#: label instead gives each match a clean start.
_ATTACH_LABEL = re.compile(r"File Attachments\s*", re.I)
_ATTACH_NAME = re.compile(
    r"(.+?\.(?:pdf|docx?|xlsx?|pptx?))\s*\(\s*[\d,]+\s*KB\s*\)", re.I
)

#: Trailing noise the preamble leaves on a subject when an item has no Category.
_SUBJECT_TAIL = re.compile(r"\s*(?:File Attachments|Recommended Action|Abstract:).*$", re.I)


@dataclass(frozen=True)
class AgendaItem:
    """One numbered item lifted out of a chunk, with its own text."""

    number: str
    subject: str
    category: str
    item_type: str
    body: str
    attachments: tuple[str, ...] = ()

    @property
    def is_consent(self) -> bool:
        return "consent" in self.item_type.lower() or "consent" in self.category.lower()


def _tidy(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" .-:" + chr(0x2013) + chr(0x2014))


def _attachments(body: str) -> tuple[str, ...]:
    """Filenames listed under this item's File Attachments label."""
    out: list[str] = []
    for lab in _ATTACH_LABEL.finditer(body):
        for m in _ATTACH_NAME.finditer(body, lab.end()):
            name = _tidy(m.group(1))
            # A name running past another label means we started mid-sentence.
            if _ATTACH_LABEL.search(name) or len(name) > 90:
                name = _tidy(_ATTACH_LABEL.split(name)[-1])
            if name and name not in out:
                out.append(name)
            if m.end() - lab.end() > 400:
                break
    return tuple(out)


def split_agenda_items(text: str) -> list[AgendaItem]:
    """Split a document's text into the agenda items it contains.

    Pass the whole document, not a chunk. See ``items_with_chunks``.

    Returns ``[]`` for text with no meeting stamp — a bare section title, a
    district-goals preamble, or minutes prose. That is the signal to fall back
    to a model rather than a defect.

    Two passes, because the spans interlock: between one item's preamble and
    the next item's stamp sit *both* the first item's body and the second
    item's subject. Locating every subject first lets each body stop where the
    next subject starts, instead of swallowing it.
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    stamps = list(_MEETING_STAMP.finditer(flat))
    if not stamps:
        return []

    # Pass 1 — where the preamble after each stamp ends.
    tails = []
    for i, st in enumerate(stamps):
        limit = stamps[i + 1].start() if i + 1 < len(stamps) else len(flat)
        tails.append(_TAIL.match(flat, st.end(), limit))

    # Pass 2 — where each subject starts, bounded by the previous preamble.
    starts = []
    floor = 0
    for i, st in enumerate(stamps):
        lo = max(floor, st.start() - _SUBJECT_LOOKBACK)
        window = flat[lo:st.start()]
        lab = _SUBJECT_LABEL.match(window)
        starts.append((lo + lab.end()) if lab else lo)
        floor = tails[i].end() if tails[i] else st.end()

    out: list[AgendaItem] = []
    for i, st in enumerate(stamps):
        lo = max(0, starts[i] - 60)
        lab = _SUBJECT_LABEL.match(flat[lo:st.start()])
        number = _tidy(lab.group("num")) if lab and lab.group("num") else ""
        subject = _SUBJECT_TAIL.sub("", _tidy(flat[starts[i]:st.start()]))
        if not subject:
            continue
        tail = tails[i]
        body_from = tail.end() if tail else st.end()
        body_to = starts[i + 1] if i + 1 < len(stamps) else len(flat)
        out.append(AgendaItem(
            number=number,
            subject=subject,
            category=_tidy(tail.group("category")) if tail else "",
            item_type=_tidy(tail.group("type")) if tail else "",
            body=flat[body_from:max(body_from, body_to)].strip(),
            attachments=_attachments(flat[body_from:max(body_from, body_to)]),
        ))
    return out


def items_with_chunks(
    doc_text: str, chunks: Sequence[tuple[str, str]]
) -> list[tuple[AgendaItem, str | None]]:
    """Items split from the whole document, each paired with the chunk citing it.

    Beats are document-level and chunks are a retrieval unit, and the two do
    not line up: the chunker splits a Greenburgh agenda so that "3. Business
    Contract: Benetech, Inc." ends one chunk and its resolution text starts the
    next. Splitting per chunk therefore saw a bare title and lost the sentence
    that says what the contract is for. Measured on the snapshot, splitting per
    document instead took Greenburgh from 593 items to 6,324 — most of the
    agenda was being dropped at chunk seams.

    Provenance still has to point at a chunk, since that is what retrieval and
    citations address, so each item is matched back to the first chunk
    containing its subject line. ``None`` means no chunk claimed it, which
    happens when the subject itself straddles a seam.

    ``chunks`` is ``(chunk_id, content)`` in document order.
    """
    normalized = [(cid, re.sub(r"\s+", " ", content or "")) for cid, content in chunks]
    out: list[tuple[AgendaItem, str | None]] = []
    cursor = 0
    for item in split_agenda_items(doc_text):
        needle = item.subject[:60]
        found: str | None = None
        for i in range(cursor, len(normalized)):
            if needle and needle in normalized[i][1]:
                found, cursor = normalized[i][0], i
                break
        else:
            for i, (cid, content) in enumerate(normalized):
                if needle and needle in content:
                    found, cursor = cid, i
                    break
        out.append((item, found))
    return out


#: What an agenda's ``Type`` implies about state *on an agenda*. An agenda says
#: what is planned, never what happened — so "Action" means an action is
#: proposed for this meeting, not that it carried. Outcomes come from minutes,
#: which is why districts holding only agendas produce threads that never end
#: (BRAIDS_PROBE.md §5).
TYPE_TO_STATE: dict[str, str] = {
    "action": "PROPOSED",
    "action (consent)": "PROPOSED",
    "discussion": "DISCUSSED",
    "presentation": "DISCUSSED",
    "information": "DISCUSSED",
    "info": "DISCUSSED",
    "report": "DISCUSSED",
    "procedural": "OTHER",
}


def state_from_type(item_type: str) -> str:
    """Prior state for an agenda item, from its declared Type."""
    return TYPE_TO_STATE.get(_tidy(item_type).lower(), "OTHER")


def state_for_item(item: AgendaItem) -> str:
    """The item's state: its Type when the district fills that in, else its text.

    Greenburgh leaves Type empty on 399 of 565 items — a property of how the
    district uses BoardDocs, not a parse failure — and the state is then
    sitting in plain sight in the body ("BE IT RESOLVED that...", "I move that
    we adjourn"). Falling back to ``normalize_state`` over the text recovers
    those without a model call.
    """
    state = state_from_type(item.item_type)
    if state != "OTHER":
        return state
    return normalize_state(f"{item.subject} {item.body[:400]}")
