"""Tests for the analytical query path (router + templated queries)."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json

import pytest

from herald import analytical
from herald.analytical import (
    RouterDecision,
    UnsupportedQuery,
    render_markdown,
    route,
    run_query,
)


class _Cur:
    """Fake cursor: returns queued fetchall result-sets in order."""

    def __init__(self, results):
        self._results = list(results)
        self.sqls: list = []

    def execute(self, sql, params=None):
        self.sqls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []


SLUGS = [("elmsford",), ("ossining",), ("peekskill",)]


# ---- router ------------------------------------------------------------

def _patch_router(monkeypatch, text: str):
    import anthropic

    class _Resp:
        content = [type("B", (), {"type": "text", "text": text})()]  # noqa: RUF012

    class _Msgs:
        async def create(self, **kw):
            return _Resp()

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Msgs()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)


def test_route_analytical(monkeypatch):
    _patch_router(monkeypatch, json.dumps({
        "mode": "analytical", "query": "step_slope",
        "params": {"lane": "MA+30", "step_from": 10, "step_to": 20, "rank": "desc"},
    }))
    d = asyncio.run(route("steepest MA+30 steps 10 to 20?", api_key="k"))
    assert d.mode == "analytical" and d.query == "step_slope"
    assert d.params["step_from"] == 10


def test_route_semantic_passthrough(monkeypatch):
    _patch_router(monkeypatch, json.dumps({"mode": "semantic", "query": None, "params": {}}))
    d = asyncio.run(route("what is the cell phone policy?", api_key="k"))
    assert d.mode == "semantic" and d.query is None


def test_route_unknown_query_falls_back_to_semantic(monkeypatch):
    _patch_router(monkeypatch, json.dumps({"mode": "analytical", "query": "bogus", "params": {}}))
    d = asyncio.run(route("weird", api_key="k"))
    assert d.mode == "semantic"


def test_route_garbage_is_semantic(monkeypatch):
    _patch_router(monkeypatch, "not json")
    d = asyncio.run(route("hello", api_key="k"))
    assert d.mode == "semantic"


# ---- query runners -----------------------------------------------------

def test_step_slope_ranks_and_reports_absence():
    # ossining present; elmsford + peekskill have no extracted grid -> not_available
    data = [("ossining", "2024-25", 92410.0, 118750.0, 26340.0, 12, "CBA 24-25",
             "https://x/cba.pdf", "contract")]
    cur = _Cur([SLUGS, data])
    dec = RouterDecision("analytical", "step_slope",
                         {"lane": "MA+30", "step_from": 10, "step_to": 20, "rank": "desc"})
    res = run_query(cur, dec)
    assert len(res.rows) == 1
    assert res.rows[0]["slug"] == "ossining" and res.rows[0]["value"] == 26340.0
    assert set(res.not_available) == {"elmsford", "peekskill"}
    # the query targets the salary table with the lane/step params
    assert any("from salary_schedule" in s for s, _ in cur.sqls)
    md = render_markdown(res)
    assert "ossining" in md and "$26,340" in md and "## Sources" in md
    assert "peekskill" in md  # listed as not available


def test_max_at_step_missing_param_is_unsupported():
    cur = _Cur([SLUGS])
    dec = RouterDecision("analytical", "max_at_step", {"lane": "MA"})  # no step
    with pytest.raises(UnsupportedQuery):
        run_query(cur, dec)


def test_stipend_compare_separates_percent_of_base():
    flat = [("ossining", "2024-25", "Head Football Coach", "", 8500.0, None, 3,
             "Stipends", "https://x/s.pdf", "contract")]
    pct = [("white-plains", "Head Football Coach", 5.0, "2024-25")]
    cur = _Cur([SLUGS, flat, pct])
    dec = RouterDecision("analytical", "stipend_compare",
                         {"position": "head football coach", "rank": "desc"})
    res = run_query(cur, dec)
    assert res.rows[0]["value"] == 8500.0
    assert res.noncomparable and res.noncomparable[0]["slug"] == "white-plains"
    md = render_markdown(res)
    assert "Percent-of-base" in md and "5% of base" in md


def test_delta_over_years_shape():
    data = [("ossining", 90000.0, 95000.0, 5000.0, "2022-23", "2024-25", 8,
             "CBA", "https://x/cba.pdf", "contract")]
    cur = _Cur([SLUGS, data])
    dec = RouterDecision("analytical", "delta_over_years", {"lane": "MA+30", "step": 10})
    res = run_query(cur, dec)
    assert res.rows[0]["value"] == 5000.0 and "2022-23→2024-25" in res.rows[0]["school_year"]


def test_lane_missing_is_unsupported():
    cur = _Cur([SLUGS])
    dec = RouterDecision("analytical", "step_slope", {"step_from": 10, "step_to": 20})
    with pytest.raises(UnsupportedQuery):
        run_query(cur, dec)


def test_render_empty_result_is_honest():
    cur = _Cur([SLUGS, []])
    dec = RouterDecision("analytical", "max_at_step", {"lane": "MA+30", "step": 10})
    res = run_query(cur, dec)
    md = render_markdown(res)
    assert "No extracted" in md
    assert res.not_available == ["elmsford", "ossining", "peekskill"]


# ---- contract currency in the citation ---------------------------------

_ON = _dt.date(2026, 9, 9)


def _max_at_step_result(doc_title: str, doc_type: str = "contract"):
    cur = _Cur([SLUGS, [("ossining", "2024-25", 79571.0, 87, doc_title,
                         "https://x/cba.pdf", doc_type)]])
    return run_query(cur, RouterDecision("analytical", "max_at_step",
                                         {"lane": "MA", "step": 3}))


def test_expired_contract_is_labelled_but_its_figure_is_kept():
    md = render_markdown(_max_at_step_result("Tarrytown-TAT-2022-2025.pdf"), on=_ON)
    assert "$79,571" in md                              # never dropped
    assert "contract expired 2025-06-30" in md          # on the ranked line
    assert "not necessarily the rate in force today" in md


def test_in_term_contract_states_its_term_only_in_the_sources_block():
    md = render_markdown(_max_at_step_result("TAT 2024-2028.pdf"), on=_ON)
    ranked, sources = md.split("## Sources")
    assert "contract in term through 2028-06-30" not in ranked
    assert "contract in term through 2028-06-30" in sources
    assert "not necessarily the rate in force today" not in md


def test_a_contract_with_no_stated_term_is_not_called_current():
    md = render_markdown(_max_at_step_result("Teachers Agreement.pdf"), on=_ON)
    assert "contract term not stated in its title" in md


def test_a_non_contract_source_gets_no_currency_verdict():
    md = render_markdown(_max_at_step_result("Budget Book 2018-2019", "budget"), on=_ON)
    assert "expired" not in md and "term through" not in md
    assert "term not stated" not in md


def test_supported_queries_constant():
    assert set(analytical.SUPPORTED_QUERIES) == {
        "step_slope", "max_at_step", "stipend_compare", "delta_over_years",
    }
