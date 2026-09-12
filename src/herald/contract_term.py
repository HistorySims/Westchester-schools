"""Whether the agreement behind a citation is still in force.

Goal A is about *current* contracts, and the corpus has no idea which of its
contracts are current: a CBA is scraped once and stays forever.
``Tarrytown-TAT-2022-2025`` — the salary schedule behind this project's first
working analytical answer — ran out on 2025-06-30, yet every answer quoting it
read as though it were the rate a teacher is paid today.

The term is read from the document's own **title**, because that is the only
place we have it. ``documents`` stores no term, and the text of a 90-page CBA
states it in prose that would cost a model call per document to recover.
District CBA filenames carry it almost universally
(``Tarrytown-TAT-2022-2025.pdf``, ``WPTA2022-2026CBA_.pdf``), which makes a
title parse cheap and auditable — and when it finds nothing we say "term not
stated", never "current".

Two spellings of the span resolve identically: ``2022-2025`` (a three-year
term) and ``2024-25`` (one school year) both end June 30 of the later year,
because NY school-district agreements run July 1 → June 30.

Expired figures are *labelled*, never dropped. A stale number that says it is
stale is useful; a missing number is not.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

# NY school-district agreements run July 1 -> June 30, so a term written
# "2022-2025" is in force through 2025-06-30 and stale from 2025-07-01.
TERM_END_MONTH, TERM_END_DAY = 6, 30

# Longest plausible CBA term. A guard, not a fact about contracts: it stops a
# pair of years that is not a term at all ("Salary Schedules 2014-2025") from
# being read as one and reported with false precision.
MAX_TERM_YEARS = 8

#: doc_type values whose currency this module has an opinion about. A policy
#: manual is current by construction (scraped live); an agenda is a dated
#: event. Only a contract can silently go out of force while still sounding
#: authoritative.
CONTRACT_DOC_TYPES = frozenset({"contract"})

# Same shape as extract_schools._TITLE_YEAR, kept separate on purpose: that one
# wants the school year a *grid* applies to, this one wants the span's end.
#
# The unseparated form is not hypothetical: Port Chester publishes its current
# agreement as `PCTA_Contract_20232027.pdf`, and with only the separated pattern
# a live 2023-2027 contract reported "term not stated". Requiring BOTH halves to
# start with "20" keeps it off timestamps — 20260930 doesn't match, because
# "0930" is not a year — and MAX_TERM_YEARS rejects what slips past.
_TERM_RES = (
    re.compile(r"(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})"),  # noqa: RUF001
    re.compile(r"(20\d{2})(20\d{2})"),
)


@dataclass(frozen=True)
class ContractTerm:
    """The span a title claims, as calendar years: 2022-2025 -> 2022, 2025."""

    start_year: int
    end_year: int

    @property
    def end_date(self) -> _dt.date:
        return _dt.date(self.end_year, TERM_END_MONTH, TERM_END_DAY)

    def expired_on(self, on: _dt.date) -> bool:
        return on > self.end_date


@dataclass(frozen=True)
class ContractStatus:
    """Citation-ready currency verdict. ``note`` reads after the word 'contract'."""

    state: str                       # 'expired' | 'current' | 'unknown'
    note: str
    term: ContractTerm | None = None

    @property
    def is_expired(self) -> bool:
        return self.state == "expired"


def parse_term(title: str) -> ContractTerm | None:
    """The contract term in a title, or None if it doesn't state one.

    Takes the FIRST span in the title, matching ``_school_year_from_title``'s
    behaviour so a document's term and its inferred school year can't disagree
    about which pair of years they read.
    """
    for pattern in _TERM_RES:
        m = pattern.search(title or "")
        if not m:
            continue
        start = int(m.group(1))
        raw_end = m.group(2)
        # '2024-25' takes the century from the start year; '2024-2027' is literal.
        end = int(raw_end) if len(raw_end) == 4 else start - start % 100 + int(raw_end)
        if end < start:
            end += 100                              # a '2099-00' style rollover
        if 0 < end - start <= MAX_TERM_YEARS:
            return ContractTerm(start_year=start, end_year=end)
    return None


def status_of(
    title: str, *, doc_type: str | None, on: _dt.date | None = None
) -> ContractStatus | None:
    """Currency of the agreement a citation rests on; None if it isn't a contract."""
    if (doc_type or "").strip().lower() not in CONTRACT_DOC_TYPES:
        return None
    term = parse_term(title)
    if term is None:
        return ContractStatus("unknown", "term not stated in its title")
    on = on or _dt.date.today()
    if term.expired_on(on):
        return ContractStatus("expired", f"expired {term.end_date.isoformat()}", term)
    return ContractStatus(
        "current", f"in term through {term.end_date.isoformat()}", term
    )


def citation_suffix(
    title: str,
    *,
    doc_type: str | None,
    on: _dt.date | None = None,
    expired_only: bool = False,
    sep: str = " — ",
) -> str:
    """The 'contract …' tail for a citation line; '' when there's nothing to say.

    ``expired_only`` is for the dense ranked lines, where "in term through
    2027-06-30" on every row is noise and "expired" on one is the point. ``sep``
    is whatever the surrounding line already separates its fields with.
    """
    st = status_of(title, doc_type=doc_type, on=on)
    if st is None or (expired_only and not st.is_expired):
        return ""
    return f"{sep}contract {st.note}"
