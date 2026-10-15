"""Assigning beats to narrative threads (docs/BRAIDS.md §2).

Three stages, cheapest and most certain first:

1. **Hard anchor** — the beat carries an identifier ("Policy 5030", "Bid
   24-017") that a live thread in the same district already carries. Free,
   exact, and immune to temporal decay, which is how a dormant issue
   reappearing years later is caught.
2. **Similarity** — the beat's *subject* embedding against each candidate
   thread's centroid, inside a temporal window. Subject, not chunk: measured
   at 95.9% nearest-neighbour topic accuracy against 74.7% for whole chunks,
   because every BoardDocs item repeats the same scaffolding and on a full
   chunk that template is most of what the vector sees.
3. **Adjudication** — a model picks from a closed shortlist. Not implemented
   here; the failure mode is a wrong pick from k options, never invented
   structure.

The bias throughout is **spawn conservatively, merge reluctantly**. A thread
created in error is cheap to merge later; a false merge corrupts two
lifecycles at once and is expensive to unpick. So anything below the floor
starts its own thread rather than being forced into the nearest one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import numpy as np

#: Identifiers worth anchoring on. Deliberately narrow — a false anchor is a
#: false merge, which is the worst outcome available, so a pattern earns its
#: place only if a collision would be surprising. Bare four-digit numbers are
#: excluded: they are also years, dollar amounts and room numbers.
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpolic(?:y|ies)\s*#?\s*(\d{3,4}(?:\.\d+)?)\b", re.I),
    re.compile(r"\b(?:bid|rfp|rfq)\s*#?\s*([\w-]{2,15}\d)\b", re.I),
    re.compile(r"\bresolution\s*#?\s*([\w-]{1,15}\d)\b", re.I),
    re.compile(r"\b(?:contract|agreement)\s*#\s*([\w-]{2,20})\b", re.I),
    re.compile(r"\bproposition\s*#?\s*(\d{1,2})\b", re.I),
)


def extract_identifiers(*texts: str) -> tuple[str, ...]:
    """Anchorable identifiers, lowercased and deduplicated, in first-seen order."""
    out: list[str] = []
    for text in texts:
        for pat in _IDENTIFIER_PATTERNS:
            for m in pat.finditer(text or ""):
                ident = m.group(1).strip().lower()
                if ident and ident not in out:
                    out.append(ident)
    return tuple(out)


#: Words that make a phrase agenda furniture rather than a subject. A phrase
#: built only from these recurs at every meeting in every district and would
#: anchor everything to everything.
_FURNITURE = frozenset((
    "accept", "acceptance", "action", "actions", "adjourn", "adjournment", "agenda",
    "allegiance", "approval", "approve", "approved", "board", "business", "call",
    "committee", "consent", "discussion", "district", "education", "items", "meeting",
    "meetings", "minutes", "motion", "new", "old", "order", "other", "pledge", "president",
    "proposed", "public", "recommendation", "regular", "report", "reports", "resolution",
    "resolved", "roll", "schools", "second", "session", "special", "superintendent",
    "work"
))

#: A named thing: two or more capitalised words, allowing lowercase joiners.
_PHRASE = re.compile(
    r"\b(?:[A-Z][\w'\-]+)(?:\s+(?:of|for|and|the|to|at|de)\s+|\s+)"
    r"(?:(?:[A-Z][\w'\-]+)(?:\s+(?:of|for|and|the|to|at|de)\s+|\s+)){0,3}"
    r"(?:[A-Z][\w'\-]+)\b"
)


#: Grammatical glue inside a name — dropped before forming n-grams so
#: "Security Vestibules MS and King Street" and "Bids for Security Vestibules"
#: share a key.
_JOINERS = frozenset(("of", "for", "and", "the", "to", "at", "de", "a", "an", "in", "on"))


def extract_key_phrases(text: str, *, n: int = 2) -> tuple[str, ...]:
    """Distinctive content n-grams usable as soft anchors.

    Numeric identifiers are rare here — 61 of 1,891 Port Chester beats carry
    one — while what actually links a storyline across changing vocabulary is a
    shared name. The security-vestibule story appears as "SEQRA Type II
    Classification for Proposed Capital Improvements - Security Vestibules and
    Lighting", then "Resolution Rejecting Bids for Security Vestibules", then
    "Rejecting Non-Responsive Bid and Awarding Contract for Security Vestibules
    MS & King Street". Embedding similarity left those as three unconnected
    single-beat threads; the bigram "security vestibules" is in all three.

    Whole phrases are the wrong unit for the same reason — they *contain* the
    shared name rather than equal it — so each named phrase is reduced to its
    content words and emitted as overlapping n-grams.

    Agenda furniture is dropped first. "Board of Education" and "Consent
    Agenda" appear at every meeting in every district and would anchor the
    corpus into a single thread.
    """
    out: list[str] = []
    for m in _PHRASE.finditer(text or ""):
        words = [
            w.lower() for w in re.split(r"[\s/]+", m.group(0))
            if w and w.lower() not in _JOINERS and w.lower() not in _FURNITURE
        ]
        for i in range(len(words) - n + 1):
            key = " ".join(words[i:i + n])
            if key not in out:
                out.append(key)
    return tuple(out)


@dataclass(frozen=True)
class Beat:
    """One agenda item, ready to be placed in a thread."""

    key: str                       # stable id: f"{chunk_id}:{subject}"
    district: str
    meeting_date: date
    subject: str
    state: str
    identifiers: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()      # soft anchors; see extract_key_phrases


@dataclass
class Thread:
    """A live thread, as assignment sees it."""

    id: str
    district: str
    title: str
    first_seen: date
    last_seen: date
    beat_count: int = 0
    identifiers: set[str] = field(default_factory=set)
    phrases: set[str] = field(default_factory=set)
    centroid: np.ndarray | None = None        # EMA, weighted toward recent beats
    centroid_first: np.ndarray | None = None  # frozen; the drift tripwire's anchor


@dataclass(frozen=True)
class Assignment:
    """Where a beat went, and on what evidence."""

    thread_id: str | None          # None means "start a new thread"
    via: str                       # 'anchor' | 'similarity' | 'adjudicate' | 'new'
    score: float = 0.0
    runner_up: float = 0.0         # best rejected score, for calibrating the floor

    @property
    def is_new(self) -> bool:
        return self.thread_id is None


#: How recently a thread must have been touched to be a similarity candidate.
#: Anchors ignore this entirely — an identifier recurring after three years is
#: exactly how a reintroduced issue surfaces.
DEFAULT_WINDOW = timedelta(days=550)

#: Cosine floor for attaching on similarity alone. Calibrate per corpus; see
#: ``calibrate_floor``. Subject embeddings in this corpus sit high and close
#: together, so a floor borrowed from another dataset will be far too low.
DEFAULT_FLOOR = 0.88

#: EMA weight for a new beat. A three-year thread's late "change order #4"
#: language does not resemble its early "should we do this", and a flat mean
#: dilutes toward the middle. The cost is that an EMA *follows* mistakes, so
#: ``centroid_first`` stays frozen as a drift tripwire.
DEFAULT_ALPHA = 0.35

#: Similarity required to attach on a *shared phrase*. Lower than the pure
#: similarity floor because the phrase is itself evidence, but not zero: a
#: shared phrase alone would merge every "budget transfer" in five years into
#: one thread. This is the spec's "merge needs an anchor, or adjudication plus
#: an overlap floor", with the phrase playing the anchor's part.
DEFAULT_PHRASE_FLOOR = 0.72

#: A phrase may anchor only if it appears in at most this share of a district's
#: beats. Rarity is what separates a project name from an agenda category, and
#: the stoplist cannot do it: "competitive bid" is not furniture in the sense
#: "pledge of allegiance" is, it is a real procurement phrase — it is simply
#: used for every procurement. Anchoring on it merged a copier lease, a
#: transportation contract, a food-service RFP and the security vestibules into
#: one 20-beat thread. "security vestibules" appears a handful of times and is
#: a name. Measured in document frequency, those are easy to tell apart.
DEFAULT_MAX_PHRASE_DF = 0.01


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def update_centroid(thread: Thread, vec: np.ndarray, *, alpha: float = DEFAULT_ALPHA) -> None:
    """Fold a beat into the thread's centroid, in place."""
    v = _unit(np.asarray(vec, dtype=np.float32))
    if thread.centroid is None:
        thread.centroid = v
        thread.centroid_first = v.copy()
    else:
        thread.centroid = _unit((1.0 - alpha) * thread.centroid + alpha * v)


def drift(thread: Thread) -> float:
    """Cosine distance between a thread's live centroid and its frozen first.

    High drift is the signature of a hijacked thread — one that started on an
    issue and absorbed an adjacent one. Recency weighting accelerates that, so
    it has to be watched rather than assumed away.
    """
    if thread.centroid is None or thread.centroid_first is None:
        return 0.0
    return 1.0 - float(np.dot(thread.centroid, thread.centroid_first))


def shortlist(
    beat: Beat,
    vec: np.ndarray,
    threads: Sequence[Thread],
    *,
    window: timedelta = DEFAULT_WINDOW,
    k: int = 3,
) -> list[tuple[Thread, float]]:
    """Top-k same-district threads by centroid similarity, inside the window."""
    v = _unit(np.asarray(vec, dtype=np.float32))
    scored: list[tuple[Thread, float]] = []
    for t in threads:
        if t.district != beat.district or t.centroid is None:
            continue
        if beat.meeting_date - t.last_seen > window:
            continue
        scored.append((t, float(np.dot(v, t.centroid))))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def assign(
    beat: Beat,
    vec: np.ndarray,
    threads: Sequence[Thread],
    *,
    floor: float = DEFAULT_FLOOR,
    phrase_floor: float = DEFAULT_PHRASE_FLOOR,
    window: timedelta = DEFAULT_WINDOW,
    rare_phrases: set[str] | None = None,
) -> Assignment:
    """Place one beat. Stages in order; the first that fires wins.

    Never returns an 'adjudicate' decision it cannot justify: when the top two
    candidates are close the caller is told, so a model can break the tie, but
    with no model the safe fallback is still a new thread.
    """
    # 1. Hard anchor — an exact identifier, free and immune to the window.
    if beat.identifiers:
        for t in threads:
            if t.district == beat.district and t.identifiers & set(beat.identifiers):
                return Assignment(t.id, "anchor", 1.0)

    # 2. Similarity.
    cands = shortlist(beat, vec, threads, window=window)
    if not cands:
        return Assignment(None, "new")
    top, score = cands[0]
    runner = cands[1][1] if len(cands) > 1 else 0.0

    # 1b. Soft anchor — a shared name, corroborated by similarity. This is what
    # carries an evolving storyline: "SEQRA ... Security Vestibules", "Rejecting
    # Bids for Security Vestibules" and "Awarding Contract for Security
    # Vestibules" share no identifier and are too far apart in embedding space
    # to clear the floor, but they share the phrase.
    want = set(beat.phrases) if rare_phrases is None else rare_phrases
    if want:
        shared = [(t, sc) for t, sc in cands if t.phrases & want and sc >= phrase_floor]
        if shared:
            t, sc = max(shared, key=lambda x: x[1])
            return Assignment(t.id, "anchor", sc, runner)

    if score >= floor:
        # Ambiguous when the runner-up is nearly as good; that is the only
        # place a model is worth paying for.
        via = "adjudicate" if runner >= floor and (score - runner) < 0.02 else "similarity"
        return Assignment(top.id, via, score, runner)
    return Assignment(None, "new", score, runner)


def calibrate_floor(
    pairs: Sequence[tuple[float, bool]], *, target_precision: float = 0.95
) -> float:
    """Lowest floor that still reaches ``target_precision`` on labelled pairs.

    ``pairs`` is (similarity, same_thread). Borrowed thresholds do not transfer
    between corpora — subject embeddings here sit between 0.64 and 0.99, so a
    floor that is generous elsewhere attaches everything to everything.
    Returns 1.0 when no threshold reaches the target, which reads as "never
    attach on similarity alone" and is the safe answer.
    """
    if not pairs:
        return DEFAULT_FLOOR
    best = 1.0
    for cut in sorted({round(s, 3) for s, _ in pairs}, reverse=True):
        hits = [same for s, same in pairs if s >= cut]
        if not hits:
            continue
        if sum(hits) / len(hits) >= target_precision:
            best = cut
        else:
            break
    return best


def phrase_document_frequency(beats: Sequence[Beat]) -> dict[str, float]:
    """Share of beats carrying each phrase, per district."""
    totals: dict[str, int] = {}
    per_district: dict[str, int] = {}
    for b in beats:
        per_district[b.district] = per_district.get(b.district, 0) + 1
        for ph in set(b.phrases):
            totals[f"{b.district}\x00{ph}"] = totals.get(f"{b.district}\x00{ph}", 0) + 1
    return {k: n / per_district[k.split("\x00")[0]] for k, n in totals.items()}


def anchorable_phrases(
    beat: Beat, df: dict[str, float], *, max_df: float = DEFAULT_MAX_PHRASE_DF
) -> set[str]:
    """The beat's phrases rare enough to be a name rather than a category."""
    return {p for p in beat.phrases if df.get(f"{beat.district}\x00{p}", 0.0) <= max_df}


@dataclass
class AuditFlags:
    """Advisory tripwires, herald-extract style — surfaced, never fatal."""

    threads: int = 0
    beats: int = 0
    single_beat_threads: int = 0
    max_drift: float = 0.0
    by_via: dict[str, int] = field(default_factory=dict)

    @property
    def zombie_rate(self) -> float:
        """Share of threads holding exactly one beat.

        High means matching is too strict: everything spawns and nothing
        attaches, which looks like success — lots of threads — and is the
        opposite.
        """
        return self.single_beat_threads / self.threads if self.threads else 0.0

    @property
    def beats_per_thread(self) -> float:
        return self.beats / self.threads if self.threads else 0.0


def audit(threads: Sequence[Thread], assignments: Sequence[Assignment]) -> AuditFlags:
    """Summarize a run so a bad threshold is visible before anything is written."""
    by_via: dict[str, int] = {}
    for a in assignments:
        by_via[a.via] = by_via.get(a.via, 0) + 1
    return AuditFlags(
        threads=len(threads),
        beats=sum(t.beat_count for t in threads),
        single_beat_threads=sum(1 for t in threads if t.beat_count == 1),
        max_drift=max((drift(t) for t in threads), default=0.0),
        by_via=by_via,
    )


def run_assignment(
    beats: Sequence[Beat],
    vectors: Sequence[np.ndarray],
    *,
    floor: float = DEFAULT_FLOOR,
    phrase_floor: float = DEFAULT_PHRASE_FLOOR,
    window: timedelta = DEFAULT_WINDOW,
    alpha: float = DEFAULT_ALPHA,
    max_phrase_df: float = DEFAULT_MAX_PHRASE_DF,
) -> tuple[list[Thread], list[Assignment]]:
    """Walk beats in date order, building threads forward.

    Chronological is not a preference: assignment links a beat to *already
    known* threads, so processing out of order would let a later beat found a
    thread its own predecessor then fails to join.
    """
    order = sorted(range(len(beats)), key=lambda i: (beats[i].meeting_date, beats[i].key))
    df = phrase_document_frequency(beats)
    threads: list[Thread] = []
    by_id: dict[str, Thread] = {}
    out: list[Assignment] = []

    for i in order:
        beat, vec = beats[i], vectors[i]
        a = assign(beat, vec, threads, floor=floor, phrase_floor=phrase_floor,
                   window=window,
                   rare_phrases=anchorable_phrases(beat, df, max_df=max_phrase_df))
        if a.is_new:
            t = Thread(
                id=f"t{len(threads) + 1:05d}",
                district=beat.district,
                title=beat.subject,
                first_seen=beat.meeting_date,
                last_seen=beat.meeting_date,
            )
            threads.append(t)
            by_id[t.id] = t
            out.append(replace(a, thread_id=t.id, via="new"))
        else:
            t = by_id[str(a.thread_id)]
            out.append(a)
        t.beat_count += 1
        t.last_seen = max(t.last_seen, beat.meeting_date)
        t.identifiers |= set(beat.identifiers)
        t.phrases |= anchorable_phrases(beat, df, max_df=max_phrase_df)
        update_centroid(t, vec, alpha=alpha)

    return threads, out
