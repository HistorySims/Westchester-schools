"""Finding where a district broke from routine in a class of compliance document.

Most of this corpus is routine. Every district must file a district-wide safety
plan, a code of conduct, a professional-development plan; the content and often
the order are mandated, so year after year seven districts file seven documents
that are mostly the same words. The interesting question is where that routine
breaks — and a document class filed by every peer under one state gives the
leverage to answer it, because a change everybody makes in the same year is
Albany, and a change one district makes alone is a decision.

**Compare sections, never whole documents.** The obvious implementation — one
mean-pooled centroid per (district, year), then cosine distance — was built,
run on 30 safety plans across seven districts, and failed (docs/DRIFT_PILOT.md).
Chapter 155.17 was amended for 2024-25 and three districts rewrote their drill
sections to match it, tripling their use of "trauma" and introducing the ban on
drills using "props, actors, simulations". Centroid distance for that transition
came out at 0.0008, the second *smallest* movement in the six-year series.

The failure is arithmetic, not embedding quality. A mandated document is ~95%
template, so a change confined to three sections of eighty moves the mean by
about a fortieth of its own length — well inside the distance between two
districts' boilerplate. Mean-pooling measures what documents have in common,
and what they have in common is the part the state wrote.

Two functions do the work:

* ``changed_sections`` pairs each section with its closest counterpart in the
  same district's previous filing and keeps the distance. Untouched template
  re-extracts at ~0.01, so a rewritten section at 0.15 stands out by an order
  of magnitude with no threshold chosen in advance. Reported as a magnitude and
  not a count on purpose: counting sections "with no near neighbour" found
  *zero* changes in 2024-25, because the mandate arrived as sentences inserted
  into sections that already existed rather than as new sections.
* ``classify_change`` asks whether a peer filed the same new language that
  year. Shared means mandate; unshared means a local decision, which is the hot
  spot worth tracing back into the agenda corpus for roots and branches.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: Running heads and bare page numbers. In a 60-page plan these repeat on every
#: page, and in a short section they are a large fraction of the text.
_FURNITURE = re.compile(
    r"Page\s+\d+\s+of\s+\d+|^\s*\d+\s*$|District[- ]?wide\s+(?:School\s+)?Safety"
    r"(?:\s*&\s*Emergency\s+Management)?\s+Plan\s*\d*",
    re.I | re.M,
)

#: Target section length in characters. Long enough to carry an argument, short
#: enough that one inserted paragraph is a visible fraction of the vector.
DEFAULT_TARGET = 1400

#: Below this a passage is a heading or a stray table row, not prose.
MIN_SECTION = 300

#: A peer filing nearly the same passage in the same year. Calibrated on the
#: 2024-25 transition, where the three districts that adopted the amended drill
#: language matched each other at 0.90-0.97 while genuinely local passages —
#: police MOUs, a district's own reunification procedure — sat at 0.67-0.79.
DEFAULT_PEER_MATCH = 0.80


@dataclass(frozen=True)
class SectionChange:
    """One section of a filing, against the closest thing the last one said."""

    index: int
    distance: float
    """Cosine distance to the nearest section of the previous filing. ~0.01 is
    re-extraction noise on unchanged template; 0.15+ is a rewrite."""
    best_peer: float
    """Highest cosine similarity to any peer district's section that year."""
    text: str

    @property
    def is_shared(self) -> bool:
        return self.best_peer > DEFAULT_PEER_MATCH


def sections(
    text: str, *, target: int = DEFAULT_TARGET, minimum: int = MIN_SECTION
) -> list[str]:
    """Split a filing into passages of roughly ``target`` characters.

    Sentence boundaries alone are not enough. A plan that is largely tables,
    rosters and checklists carries almost no terminal punctuation, so a single
    "sentence" can run for thousands of characters: Peekskill's 2020-21 plan
    produced 26 sections from 87,000 characters where its own 2021-22 produced
    63. Those sections are not comparable to anybody else's, and mean-pooling a
    handful of enormous vectors is not the centroid that pooling sixty ordinary
    ones gives. So an overlong run is cut on whitespace at the target.
    """
    body = re.sub(r"\s+", " ", _FURNITURE.sub(" ", text or "")).strip()
    if not body:
        return []

    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.;:])\s+", body):
        while len(sentence) > target * 1.6:
            cut = sentence.rfind(" ", 0, target)
            if cut <= target // 2:
                cut = target
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if sentence:
            pieces.append(sentence)

    out: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > target:
            out.append(current.strip())
            current = ""
        current += piece + " "
    if current.strip():
        out.append(current.strip())
    return [s for s in out if len(s) > minimum]


def _unit_rows(vectors: np.ndarray) -> np.ndarray:
    v = np.asarray(vectors, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def changed_sections(
    current: np.ndarray,
    previous: np.ndarray,
    texts: Sequence[str],
    *,
    peers: np.ndarray | None = None,
) -> list[SectionChange]:
    """Each section of this filing, ranked by how far it moved, most first.

    ``current`` and ``previous`` are section embeddings for the same district in
    adjacent filings; ``peers`` are every other district's sections from the
    same year, used only to label a change as shared or local. Vectors need not
    arrive normalized.

    With no previous filing to compare against, every section reads as maximally
    changed — which is true but useless, so the caller is expected to skip a
    district's first year rather than this function inventing a baseline.
    """
    if len(texts) != len(current):
        raise ValueError(f"{len(current)} vectors but {len(texts)} texts")
    if not len(current) or not len(previous):
        return []

    cur = _unit_rows(current)
    prev = _unit_rows(previous)
    distance = 1.0 - (cur @ prev.T).max(axis=1)

    if peers is not None and len(peers):
        best_peer = (cur @ _unit_rows(peers).T).max(axis=1)
    else:
        best_peer = np.zeros(len(cur), dtype=np.float32)

    changes = [
        SectionChange(index=i, distance=float(distance[i]),
                      best_peer=float(best_peer[i]), text=texts[i])
        for i in range(len(cur))
    ]
    return sorted(changes, key=lambda c: -c.distance)


def classify_change(change: SectionChange, *, peer_match: float = DEFAULT_PEER_MATCH) -> str:
    """``"mandate"``, ``"local"`` or ``"unchanged"`` for one section.

    "Unchanged" covers the great majority: a filing that re-states last year's
    template, where the distance is re-extraction noise. Of the two that remain,
    ``local`` is the one worth a human's attention — on the safety plans it
    surfaced inter-agency police agreements, a private security contract and one
    district's own student-reunification design, none of which any peer filed.

    **This label is calibrated per document class and does not transfer; the
    distance ranking does.** On safety plans the separation is wide, because
    districts absorbing a regulation paste near-identical text and agree at
    0.90-0.97 while genuinely local passages sit at 0.67-0.79. Run against codes
    of conduct it broke: Greenburgh's section implementing the §2803
    phone-free-schools law — the clearest mandate in that corpus — matched peers
    at 0.79 and came back ``local``.

    The cause is structural rather than a threshold worth nudging. Ossining
    implemented the same law by writing a *separate* policy (5695) and
    cross-referencing it from the code, so there was no comparable section to
    agree with; what agreement there was came from generic disciplinary
    language. A within-class peer test asks whether two districts changed the
    same document in the same way, and for a mandate that is not guaranteed.
    Prefer reporting ``best_peer`` to a caller over reporting this label, and
    re-measure the gate on any new class before trusting it.
    """
    if change.distance < 0.12:
        return "unchanged"
    return "mandate" if change.best_peer > peer_match else "local"


def consensus_movement(
    year_a: dict[str, np.ndarray], year_b: dict[str, np.ndarray]
) -> float:
    """How far the peer group as a whole moved between two years.

    Kept because it is the right *question* even though centroid distance was
    the wrong instrument for answering it per-district: with sections already
    filtered down to the ones that changed, the mean of those is meaningful
    where the mean of a whole document was not. Each argument maps district to
    that district's section embeddings for the year; districts missing from
    either side are ignored, since a filing nobody can be compared against
    tells us nothing about the group.
    """
    shared = sorted(set(year_a) & set(year_b))
    if len(shared) < 2:
        return 0.0
    def centroid(year: dict[str, np.ndarray]) -> np.ndarray:
        per_district = [_unit_rows(year[d]).mean(axis=0) for d in shared]
        return _unit_rows(np.mean(per_district, axis=0))[0]
    return float(1.0 - centroid(year_a) @ centroid(year_b))
