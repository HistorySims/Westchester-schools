"""Comparing the same board policy across districts.

Board policies are licensed, not written. The districts buy model policy from
the same few vendors, so two manuals run word-for-word identical for pages and
then differ in the one figure that decides something: 18 absences before a
student loses course credit, or 20, or no figure at all because the call is left
to an administrator.

**This is the case embedding distance cannot see.** "eighteen (18) absences"
against "twenty (20) absences" in otherwise identical text sits at cosine
~0.999 — closer than two paraphrases of the *same* rule. The section-distance
method in ``drift_schools`` is built for the opposite problem, prose that was
rewritten, and on a policy manual it would report two districts as agreeing
precisely where they disagree most. A number inside shared boilerplate is an
exact-extraction problem, and absence is a set difference.

Two complementary tools, because neither covers the other's cases:

``divergences``
    A broad sweep. Align sentences between two districts' copies of one policy,
    keep the pairs whose *wording* matches but whose quantities do not. Needs no
    question asked in advance and no threshold to calibrate, and it finds things
    nobody was looking for — Tarrytowns requires election petitions 90 days
    ahead where White Plains requires 60. It only works where the vendor's
    sentence survived in both copies.

``rule_parameter``
    A targeted probe. Name the *consequence* ("loss of credit") and read the
    quantity stated near it, whatever the surrounding wording. Necessary
    precisely where the sweep fails: Greenburgh writes "any student with more
    than 18 absences in a course may not receive credit" and Mount Vernon a
    bulleted "20 absences for a one (1) credit course", which no sentence
    alignment will pair.

A parameter is a quantity **with a unit**. Comparing bare numbers instead buries
every real finding under citation formatting, where "8 N.Y.C.R.R. 200.2(c)" and
"(8NYCRR 200.2(c))" are one citation tokenized two ways and "Version 2" against
"Version 2.0" is nothing at all. Requiring a unit discards all of it without a
single special case.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Units that make a number a parameter rather than a citation or a year.
UNIT = (
    r"(?:school\s+|working\s+|business\s+|calendar\s+|instructional\s+)?"
    r"(?:days?|weeks?|months?|years?|hours?|minutes?"
    r"|absences?|ATEDs?|tardi(?:es|ness)|class(?:es|\s+periods?)?"
    r"|students?|members?|credits?|percent|%|votes?|signatures?)"
)

#: Words that sit between the number and its unit. "18 unexcused ATEDs for a
#: full year" is Tarrytowns' entire credit rule, and without this the number
#: and the unit never meet.
_MODIFIER = r"(?:(?:or\s+more|unexcused|excused|consecutive|cumulative|full|half)\s+)*"

#: "within 10 working days", "more than 18 absences", "thirty (30) school days".
_QUANTITY = re.compile(
    r"\b(\d{1,3})\s*(?:\(\s*\d{1,3}\s*\)\s*)?" + _MODIFIER + r"(" + UNIT + r")\b", re.I
)
#: These documents spell a number out and repeat it in figures — "eighteen
#: (18) absences" — so the figure in the brackets carries the value while the
#: word defeats alignment when only one district spells it out.
_SPELLED = re.compile(
    r"\(\s*(\d{1,3})\s*\)\s*" + _MODIFIER + r"(" + UNIT + r")\b", re.I
)

_SENTENCE = re.compile(r"(?<=[.;:])\s+")
_DIGITS = re.compile(r"\b\d{1,3}\b")
_PARENTHETICAL = re.compile(r"\s*\(\s*\d{1,3}\s*\)")

#: Spelled-out numbers have to be blanked alongside the figures, or a clause one
#: district writes as "12 months" and the other as "six (6) months" gets two
#: different wording keys and never aligns — the figures match after blanking
#: and the leftover word does not.
_NUMBER_WORD = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twent|thirt|fort|fift|sixt|sevent|eight|ninet)"
    r"(?:y|ieth|eth|ies)?(?:[-\s](?:one|two|three|four|five|six|seven|eight|nine))?\b",
    re.I,
)

#: Wording this close is the same clause out of the same model policy. Measured
#: on the four committed manuals: real vendor clauses land at 0.98-1.00, and
#: below about 0.86 the pairs are different rules that share vocabulary.
SAME_WORDING = 0.86

#: Shortest clause worth aligning. Below this, boilerplate fragments match each
#: other indiscriminately.
MIN_CLAUSE = 60


#: Units are lowercased so they compare, but one of them is an acronym and
#: "18 ateds" in a report is not what the policy says.
_UNIT_CASING = {"ated": "ATED"}


@dataclass(frozen=True)
class Quantity:
    """A number that measures something, with the unit that says what."""

    value: int
    unit: str

    def __str__(self) -> str:
        unit = _UNIT_CASING.get(self.unit, self.unit)
        return f"{self.value} {unit}{'' if self.value == 1 else 's'}"


@dataclass(frozen=True)
class Divergence:
    """One clause two districts share, and the quantities they disagree on."""

    policy: str
    title: str
    district_a: str
    district_b: str
    wording: float
    """Similarity of the clause with its numbers blanked. 1.0 is identical."""
    quantities_a: tuple[Quantity, ...]
    quantities_b: tuple[Quantity, ...]
    clause_a: str
    clause_b: str

    @property
    def kind(self) -> str:
        """``"value"`` when the numbers differ, ``"unit"`` when only the unit does.

        Both matter and the second is easier to miss by eye: Greenburgh gives a
        board 30 *school* days to arrange a private placement where Mount Vernon
        gives it 30 days, which is the same number and about six weeks apart.
        """
        if {q.value for q in self.quantities_a} != {q.value for q in self.quantities_b}:
            return "value"
        return "unit"


def policy_number(title: str) -> str:
    """"7110" from "7110 Comprehensive Student Attendance Policy".

    Districts do not agree on which number a subject gets — attendance is 7110
    in Greenburgh and Mount Vernon and 5100 in Tarrytowns and White Plains — so
    this aligns the copies that *do* share a number and the rest need matching
    by title or by hand.
    """
    m = re.match(r"\s*(\d{3,4}(?:\.\d+)?(?:-[A-Za-z]+(?:\.\d+)?)?)", title or "")
    return m.group(1) if m else ""


def clauses(text: str, *, minimum: int = MIN_CLAUSE, maximum: int = 600) -> list[str]:
    """Sentence-ish units of a policy, long enough to be a rule."""
    flat = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in _SENTENCE.split(flat) if minimum < len(s.strip()) < maximum]


def wording_key(clause: str) -> str:
    """The clause with every number removed — what is left is its wording."""
    s = _PARENTHETICAL.sub(" ", (clause or "").lower())
    s = _DIGITS.sub("#", s)
    s = _NUMBER_WORD.sub("#", s)
    s = re.sub(r"[^a-z#\s]", " ", s)
    # Blanking both forms can leave "# #" where one district wrote a word and
    # figures and the other wrote figures alone, and removing a parenthetical
    # leaves the space that was in front of it.
    s = re.sub(r"(?:#\s+)+#", "#", s)
    return re.sub(r"\s+", " ", s).strip()


def quantities(clause: str) -> tuple[Quantity, ...]:
    """Every quantity stated with a unit, de-duplicated, in value order."""
    out: list[Quantity] = []
    seen: set[tuple[int, str]] = set()
    for pattern in (_QUANTITY, _SPELLED):
        for m in pattern.finditer(clause or ""):
            unit = re.sub(r"\s+", " ", m.group(2).lower()).rstrip("s")
            key = (int(m.group(1)), unit)
            if key not in seen:
                seen.add(key)
                out.append(Quantity(value=key[0], unit=unit))
    return tuple(sorted(out, key=lambda q: (q.value, q.unit)))


def divergences(
    policy: str,
    title: str,
    district_a: str,
    clauses_a: Sequence[str],
    district_b: str,
    clauses_b: Sequence[str],
    *,
    same_wording: float = SAME_WORDING,
) -> list[Divergence]:
    """Clauses the two districts word the same way and quantify differently.

    Ordered most-identical first, because a clause the two districts share
    verbatim but for one figure is a deliberate local choice, while one at 0.87
    may just be two vendors' phrasings of the same statute.
    """
    index_b = {wording_key(c): c for c in clauses_b}
    keys_b = list(index_b)
    out: list[Divergence] = []
    for clause in clauses_a:
        key = wording_key(clause)
        qa = quantities(clause)
        if not qa or len(key) < MIN_CLAUSE:
            continue
        near = difflib.get_close_matches(key, keys_b, n=1, cutoff=same_wording)
        if not near:
            continue
        other = index_b[near[0]]
        qb = quantities(other)
        if not qb or qa == qb:
            continue
        out.append(Divergence(
            policy=policy, title=title,
            district_a=district_a, district_b=district_b,
            wording=difflib.SequenceMatcher(None, key, near[0]).ratio(),
            quantities_a=qa, quantities_b=qb,
            clause_a=clause, clause_b=other,
        ))
    return sorted(out, key=lambda d: -d.wording)


@dataclass(frozen=True)
class RuleParameter:
    """What a district set for one rule, or that it set no number at all."""

    district: str
    policy: str
    value: int | None
    unit: str
    quote: str

    def __str__(self) -> str:
        if self.value is None:
            return "no numeric threshold"
        return str(Quantity(self.value, self.unit))

    @property
    def is_discretionary(self) -> bool:
        """No figure in the policy. Not the same as having no policy.

        White Plains states that "the decision to deny course credit to a
        student will be made by the House Administrator", with an appeal to the
        Principal. That is a discretionary standard rather than a missing rule,
        and reporting the two the same way would be wrong in both directions.
        """
        return self.value is None


#: Ways a policy says a student loses course credit. Kept here rather than in
#: the CLI because it is domain vocabulary that took reading eight manuals to
#: get right, and because each addition came from a district the previous
#: version got wrong:
#:
#: * "will not receive credit **for that course**" — four districts phrase it
#:   this way; requiring "course credit" adjacent read Tarrytowns, Port Chester
#:   and Elmsford as having no rule when each states an explicit threshold.
#: * "may be **denied** credit" — Peekskill's active voice. It also carries a
#:   discretion clause, so missing this did not merely lose the figure, it
#:   reported the district as having no bright line at all.
CREDIT_DENIAL = re.compile(
    r"(?:(?:may|will|shall|can)\s+(?:not\s+)?(?:be\s+)?"
    r"(?:receive|earn|be\s+given|denied|deny)(?:\s+course)?\s+credit"
    r"|credit\s+will\s+not\s+be\s+(?:given|granted|earned|awarded)"
    r"|no\s+credit|denial\s+of\s+course\s+credit|denied\s+credit"
    r"|loss\s+of\s+credit|lose\s+credit|withhold\w*\s+credit"
    r"|forfeit\w*\s+credit)",
    re.I,
)

#: Other consequences that live in the same paragraphs and carry their own
#: numbers: discharge from enrollment, a CPS referral, summer-school
#: ineligibility, suspension. See ``rule_parameter``'s ``exclude``.
COMPETING_CONSEQUENCE = re.compile(
    r"discharg|CPS|child\s+protective|summer\s+school|suspen|detention"
    r"|expel|truan",
    re.I,
)

#: A district may set no figure and leave the call to a person. White Plains
#: holds the same vendor template as four peers with the threshold sentence
#: simply absent: "the decision to deny course credit to a student will be made
#: by the House Administrator". That is a discretionary standard, not a missing
#: policy, and the two must not be reported alike.
CREDIT_DISCRETION = re.compile(
    r"decision\s+to\s+deny\s+course\s+credit[^.]{0,80}will\s+be\s+made\s+by"
    r"|at\s+the\s+discretion\s+of\s+the\s+(?:principal|administrator|"
    r"house\s+administrator)",
    re.I,
)

#: Units a credit threshold is stated in. "ATED" is New York's term of art for
#: Absence, Tardiness or Early Departure and four districts state the whole
#: rule in it.
CREDIT_UNITS = ("absence", "ated", "day", "class", "class period")


def _nearest(position: int, matches: Sequence[tuple[int, int]]) -> int:
    """Characters from ``position`` to the closest span, or a large number."""
    if not matches:
        return 1 << 30
    return min(min(abs(position - start), abs(position - end))
               for start, end in matches)


def rule_parameter(
    district: str,
    policy: str,
    text: str,
    *,
    consequence: re.Pattern[str],
    units: Iterable[str] = (),
    exclude: re.Pattern[str] | None = None,
    window: int = 260,
    plausible: tuple[int, int] = (10, 60),
) -> RuleParameter | None:
    """The quantity attached to ``consequence``, or None where none is stated.

    ``window`` is how far from the consequence phrase a number still counts as
    its parameter — one sentence either side. Wider, and an escalation ladder's
    earlier rungs get read as the threshold: Greenburgh contacts a parent at 4
    absences, writes a letter at 9, opens a contract at 13 and denies credit at
    18, all within a few hundred characters. The largest in range is taken,
    because these rules are written as ladders whose last rung is the denial,
    and because the half-credit variant is always the smaller number.

    ``exclude`` is what makes that safe to do. A policy paragraph often states
    several different rules, and the largest nearby number may belong to one of
    the others: Mount Vernon's attendance policy puts "a student with 20
    consecutive absences … can be discharged" a couple of sentences from its
    "no credit" language, so without an exclusion the tool reported the
    discharge rule's figure with the discharge sentence as its evidence. It
    happened to equal the real credit threshold, which is the worst way for a
    bug to present.

    A number is attributed to whichever consequence is *nearest* to it, rather
    than dropped for having a competing phrase somewhere nearby. A fixed window
    cannot do this job: the discharge verb sits about 75 characters past its
    number, and widening the window far enough to catch it also reaches the
    "summer school" clause that follows the genuine credit table, so the real
    threshold would be discarded instead. Distance decides, and the quote kept
    is the text around the winning number rather than around the phrase.
    """
    flat = re.sub(r"\s+", " ", text or "")
    wanted = {u.lower().rstrip("s") for u in units}
    targets = [(m.start(), m.end()) for m in consequence.finditer(flat)]
    competing = ([(m.start(), m.end()) for m in exclude.finditer(flat)]
                 if exclude is not None else [])
    if not targets:
        return None

    # Both patterns, because these policies state a threshold either way and
    # often both ways in one paragraph: "20 absences for a one (1) credit
    # course" needs the first, "eighteen (18) absences" only matches the
    # second. Reading one pattern found no threshold at all in the districts
    # that spell their numbers out.
    spans: dict[tuple[int, int], tuple[int, str]] = {}
    for pattern in (_QUANTITY, _SPELLED):
        for m in pattern.finditer(flat):
            unit = re.sub(r"\s+", " ", m.group(2).lower()).rstrip("s")
            spans.setdefault((m.start(), m.end()), (int(m.group(1)), unit))

    best: tuple[int, str, str] | None = None
    for (start, end), (value, unit) in sorted(spans.items()):
        if not plausible[0] <= value <= plausible[1]:
            continue
        if wanted and unit not in wanted:
            continue
        to_target = _nearest(start, targets)
        if to_target > window:
            continue
        if competing and _nearest(start, competing) <= to_target:
            continue
        if best is None or value > best[0]:
            lo, hi = max(0, start - window), min(len(flat), end + window)
            best = (value, unit, flat[lo:hi].strip())
    if best is None:
        return None
    return RuleParameter(district=district, policy=policy,
                         value=best[0], unit=best[1], quote=best[2])
