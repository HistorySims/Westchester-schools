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
from dataclasses import dataclass

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
_ITEM = re.compile(
    r"Subject\s+(?P<num>\d+(?:\.\d+)*)?\s*(?P<subject>.+?)\s+"
    r"Meeting\s+\w{3}\s+\d{1,2},\s+\d{4}\s*-\s*"
    # The meeting name runs until the next labelled field. Without these
    # lookaheads the optional Category/Type groups are trivially satisfied by
    # matching nothing, the meeting name swallows them, and every item comes
    # back with no Type — which silently makes every state OTHER.
    r"(?P<meeting>.+?)(?=\s+Category\s|\s+Type\s|\s+Subject\s|$)"
    r"(?:\s+Category\s+(?P<category>.+?)(?=\s+Type\s|\s+Subject\s|$))?"
    r"(?:\s+Type\s+(?P<type>Action \(Consent\)|Action|Discussion|Information|"
    # `\\b` cannot match after "Action (Consent)" — a ")" followed by a space is
    # not a word boundary, so the engine backtracked to bare "Action" and every
    # consent item quietly lost its consent flag.
    r"Procedural|Report|Presentation|Info)(?=[\s,]|$)[, ]*)?",
    re.I,
)

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
    """Split one chunk's text into the agenda items it contains.

    Returns ``[]`` for text with no item preamble — a meeting header, a bare
    section title, or minutes prose. That is the signal to fall back to a model
    rather than a defect.
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    matches = list(_ITEM.finditer(flat))
    if not matches:
        return []

    out: list[AgendaItem] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(flat)
        body = flat[m.end():end].strip()
        subject = _SUBJECT_TAIL.sub("", _tidy(m.group("subject")))
        if not subject:
            continue
        out.append(AgendaItem(
            number=_tidy(m.group("num")),
            subject=subject,
            category=_tidy(m.group("category")),
            item_type=_tidy(m.group("type")),
            body=body,
            attachments=_attachments(body),
        ))
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
