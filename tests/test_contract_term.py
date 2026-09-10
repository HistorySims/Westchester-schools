"""Tests for contract-term parsing and the currency labels it produces."""

from __future__ import annotations

import datetime as _dt

from herald.contract_term import (
    ContractTerm,
    citation_suffix,
    parse_term,
    status_of,
)

TODAY = _dt.date(2026, 9, 9)


# ---- parsing -----------------------------------------------------------

def test_multi_year_term_from_a_real_filename():
    # the document behind this project's first working analytical answer
    term = parse_term("Tarrytown-TAT-2022-2025.pdf")
    assert term is not None
    assert term == ContractTerm(start_year=2022, end_year=2025)
    assert term.end_date == _dt.date(2025, 6, 30)


def test_two_digit_end_takes_the_century_from_the_start():
    assert parse_term("Teacher Agreement 2024-25") == ContractTerm(2024, 2025)


def test_span_without_separators_around_it():
    # White Plains publishes it as WPTA2022-2026CBA_.pdf — no word boundaries
    assert parse_term("WPTA2022-2026CBA_.pdf") == ContractTerm(2022, 2026)


def test_en_dash_and_slash_spans():
    assert parse_term("CBA 2021–2024") == ContractTerm(2021, 2024)  # noqa: RUF001
    assert parse_term("CBA 2021 / 2024") == ContractTerm(2021, 2024)


def test_unseparated_span_from_a_real_filename():
    # Port Chester publishes its current CBA as PCTA_Contract_20232027.pdf; with
    # only the separated pattern a live contract reported "term not stated".
    assert parse_term("PCTA_Contract_20232027.pdf") == ContractTerm(2023, 2027)


def test_unseparated_pattern_does_not_eat_timestamps():
    # 8-digit dates are the obvious false positive; the second half is not a year
    assert parse_term("minutes_20260930.pdf") is None
    assert parse_term("scan_20240115_final.pdf") is None
    assert parse_term("id 20202020") is None          # zero-length span


def test_a_separated_span_wins_over_a_trailing_stamp():
    assert parse_term("Agreement 2023-2027 rev 20240115") == ContractTerm(2023, 2027)


def test_no_span_is_not_a_term():
    assert parse_term("Collective Bargaining Agreement.pdf") is None
    assert parse_term("") is None
    assert parse_term("Policy 1500-2019") is None      # first year isn't 20xx


def test_implausibly_long_span_is_rejected_rather_than_guessed():
    # "Salary Schedules 2014-2025" is a range of documents, not a term
    assert parse_term("Salary Schedules 2014-2025") is None
    assert parse_term("Agreement 2024-2024") is None   # zero-length


def test_first_span_wins_matching_the_school_year_parser():
    assert parse_term("2022-2025 CBA (supersedes 2019-2022)") == ContractTerm(2022, 2025)


# ---- status ------------------------------------------------------------

def test_expired_contract_says_so_with_its_end_date():
    st = status_of("Tarrytown-TAT-2022-2025.pdf", doc_type="contract", on=TODAY)
    assert st is not None and st.state == "expired" and st.is_expired
    assert st.note == "expired 2025-06-30"


def test_contract_still_in_term():
    st = status_of("Teachers Agreement 2024-2028.pdf", doc_type="contract", on=TODAY)
    assert st is not None and st.state == "current" and not st.is_expired
    assert st.note == "in term through 2028-06-30"


def test_white_plains_cba_ran_out_this_summer():
    # WPTA2022-2026 was the newest CBA in data/targets/cba_sources.json and it
    # expired 2026-06-30 — the labelling is not a Tarrytown-only concern.
    st = status_of("WPTA2022-2026CBA_.pdf", doc_type="contract", on=TODAY)
    assert st is not None
    assert st.state == "expired" and st.note == "expired 2026-06-30"


def test_the_last_day_of_the_term_is_still_in_force():
    # NY agreements run July 1 -> June 30: June 30 is in, July 1 is out
    def state_on(d: _dt.date) -> str:
        st = status_of("CBA 2022-2025", doc_type="contract", on=d)
        assert st is not None
        return st.state

    assert state_on(_dt.date(2025, 6, 30)) == "current"
    assert state_on(_dt.date(2025, 7, 1)) == "expired"


def test_untitled_term_is_unknown_never_current():
    st = status_of("Teachers Agreement.pdf", doc_type="contract", on=TODAY)
    assert st is not None and st.state == "unknown" and not st.is_expired
    assert "not stated" in st.note


def test_non_contracts_have_no_verdict():
    # a policy manual is current by construction; an agenda is a dated event
    assert status_of("Code of Conduct 2019-2020", doc_type="policy", on=TODAY) is None
    assert status_of("Budget 2018-2019", doc_type="budget", on=TODAY) is None
    assert status_of("x", doc_type=None, on=TODAY) is None


# ---- citation suffix ---------------------------------------------------

def test_citation_suffix_uses_the_line_s_own_separator():
    assert citation_suffix("CBA 2022-2025.pdf", doc_type="contract", on=TODAY) == (
        " — contract expired 2025-06-30"
    )
    assert citation_suffix("CBA 2022-2025.pdf", doc_type="contract", on=TODAY,
                           sep=" · ") == " · contract expired 2025-06-30"


def test_expired_only_drops_the_in_term_noise():
    assert citation_suffix("CBA 2022-2027.pdf", doc_type="contract", on=TODAY,
                           expired_only=True) == ""
    assert citation_suffix("CBA 2022-2025.pdf", doc_type="contract", on=TODAY,
                           expired_only=True) != ""
    assert citation_suffix("Agenda", doc_type="agenda", on=TODAY) == ""
