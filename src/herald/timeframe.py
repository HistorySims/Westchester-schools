"""Time expressions in a question — resolved when they can be, flagged when not.

A question can carry a timeframe the retrieval never sees. *"How much did each
district pay Google last year?"* was answered from evidence dated 2013 to
2026, because ``--since`` was empty and nothing looked at the question. The
answer was not wrong so much as unscoped, and nothing said so.

Two kinds of expression, treated differently on purpose:

* **Resolvable** — "in 2024", "the 2023-24 school year", "since 2022". These
  have one meaning; apply them.
* **Vague** — "last year", "recently", "currently". These do *not* have one
  meaning here. In August, "last year" might be the 2025 calendar year or the
  2025-26 school year, and a district's budget documents are organised by the
  latter. Guessing silently is the mistake this project keeps finding, so
  these are detected and **reported**, never resolved.

A school year runs July 1 → June 30 (NY), which is why "2023-24" is not the
same window as "2023".
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

#: July 1 — the day a New York school year starts.
SCHOOL_YEAR_START = (7, 1)

# "2023-24", "2023-2024", "2023/24", optionally followed by "school year"
_SCHOOL_YEAR = re.compile(
    r"\b(20\d{2})\s*[-/\u2013]\s*(\d{2}|20\d{2})\b(?:\s*school\s*year)?", re.I
)
_SINCE_YEAR = re.compile(r"\b(?:since|after|from)\s+(20\d{2})\b", re.I)
_BEFORE_YEAR = re.compile(r"\b(?:before|prior\s+to|up\s+to|until)\s+(20\d{2})\b", re.I)
_IN_YEAR = re.compile(r"\b(?:in|during|for)\s+(20\d{2})\b", re.I)
_BARE_YEAR = re.compile(r"\b(20\d{2})\b")

#: Expressions that clearly mean "scope this in time" but not *which* time.
_VAGUE = re.compile(
    r"\b(last|past|previous|prior)\s+(year|school\s*year|few\s+years|couple\s+of\s+years)\b"
    r"|\b(this|current|the\s+current)\s+(year|school\s*year)\b"
    r"|\b(recent(ly)?|currently|nowadays|these\s+days|to\s*date|so\s+far)\b"
    r"|\b(latest|most\s+recent|newest)\b",
    re.I,
)


@dataclass(frozen=True)
class Timeframe:
    """A time expression found in a question."""

    phrase: str
    date_from: _dt.date | None = None
    date_to: _dt.date | None = None

    @property
    def resolved(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    def describe(self) -> str:
        if not self.resolved:
            return f'"{self.phrase}"'
        a = self.date_from.isoformat() if self.date_from else "any"
        b = self.date_to.isoformat() if self.date_to else "any"
        return f'"{self.phrase}" → {a} .. {b}'


def _end_year(start: int, tail: str) -> int:
    """"2023-24" → 2024; "2023-2024" → 2024."""
    return int(tail) if len(tail) == 4 else int(str(start)[:2] + tail)


def detect_timeframe(question: str) -> Timeframe | None:
    """The timeframe a question asks for, if it asks for one.

    Returns ``None`` when the question is timeless. A returned frame with
    ``resolved`` False means "the asker scoped this and we could not" — which
    the caller must surface rather than quietly ignore.
    """
    q = question or ""

    m = _SCHOOL_YEAR.search(q)
    if m:
        start, end = int(m.group(1)), _end_year(int(m.group(1)), m.group(2))
        return Timeframe(
            phrase=m.group(0).strip(),
            date_from=_dt.date(start, *SCHOOL_YEAR_START),
            date_to=_dt.date(end, *SCHOOL_YEAR_START) - _dt.timedelta(days=1),
        )

    m = _SINCE_YEAR.search(q)
    if m:
        return Timeframe(m.group(0).strip(), date_from=_dt.date(int(m.group(1)), 1, 1))

    m = _BEFORE_YEAR.search(q)
    if m:
        return Timeframe(m.group(0).strip(), date_to=_dt.date(int(m.group(1)), 1, 1)
                         - _dt.timedelta(days=1))

    m = _IN_YEAR.search(q) or _BARE_YEAR.search(q)
    if m:
        year = int(m.group(1) if m.re is _IN_YEAR else m.group(0))
        return Timeframe(m.group(0).strip(),
                         date_from=_dt.date(year, 1, 1), date_to=_dt.date(year, 12, 31))

    m = _VAGUE.search(q)
    if m:
        # Detected, deliberately not resolved. See the module docstring.
        return Timeframe(phrase=m.group(0).strip())
    return None


def span_of(dates: list[_dt.date | None]) -> tuple[_dt.date, _dt.date] | None:
    """Earliest and latest real date in a list, ignoring undated items."""
    real = sorted(d for d in dates if d is not None)
    return (real[0], real[-1]) if real else None


def scope_note(
    frame: Timeframe | None,
    *,
    applied: bool,
    evidence_span: tuple[_dt.date, _dt.date] | None,
) -> str:
    """One line for the answer saying how time was handled — or that it wasn't.

    Written for the reader of the answer, not the operator: someone who asked
    about "last year" needs to know, in the answer itself, that the evidence
    behind it spans a decade.
    """
    if frame is None:
        return ""
    span = ""
    if evidence_span:
        a, b = evidence_span
        span = (f" Evidence shown is dated {a.isoformat()}"
                + ("" if a == b else f" to {b.isoformat()}") + ".")
    if applied and frame.resolved:
        return f"_Scoped to {frame.describe()}._{span}"
    return (
        f'_**Not scoped in time.** The question says {frame.describe()}, which is '
        f"ambiguous here — a district's documents run on a school year (July-June), "
        f"so it was NOT applied as a filter.{span} Re-run with an explicit "
        f"`since` / `until` to scope it._"
    )
