"""The analytical query path (docs/STRUCTURED.md §4).

RAG can't answer parametric questions over the salary/stipend grids
(*"steepest MA+30 step 10→20 across districts"*). This module answers them with
a **query**, not retrieval:

1. **Router** — a cheap Haiku call classifies a question as ``semantic`` (→
   today's panel RAG) or ``analytical``, and for analytical ones fills a
   template ``{query, params}``.
2. **Templated queries** — the SQL for our handful of shapes is *static and
   hand-written* (step-slope ranking, max-at-step, stipend comparison,
   delta-over-years). The router output is just its parameters. The failure mode
   is "unsupported question", never plausible-but-wrong SQL.
3. **Answer** — the ranking is computed in SQL (trustworthy numbers); the result
   is rendered as a cited, ranked answer. Districts with no extracted grid are
   listed as "not available" (honest absence); percent-of-base stipends are
   reported separately as non-comparable.

Numbers never pass through the model — only the framing does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from herald.extract_schools import parse_model_json
from herald.taxonomy import normalize_lane

ROUTER_MODEL = "claude-haiku-4-5-20251001"

SUPPORTED_QUERIES = ("step_slope", "max_at_step", "stipend_compare", "delta_over_years")

# Sanity band shared with the extractor's audit — used only for a caveat.
SALARY_MIN, SALARY_MAX = 30_000, 250_000


ROUTER_SYSTEM = """\
You route a question about Westchester school-district data to either RAG or a
structured query. Output ONLY a JSON object (no prose, no fences):

{"mode": "analytical" | "semantic", "query": <name|null>, "params": {...}}

Use "analytical" ONLY when the question is a computation over teacher SALARY
schedules or coach/extra-duty STIPEND schedules that fits one of these shapes;
otherwise use "semantic" (query null, params {}).

Shapes and their params:
- "step_slope": how much salary rises in a lane between two steps/years, ranked
  across districts. params: {"lane": "<e.g. MA+30>", "step_from": <int>,
  "step_to": <int>, "rank": "desc"|"asc"}. ("steepest" ⇒ desc.)
- "max_at_step": highest/lowest salary at one lane+step across districts.
  params: {"lane": "<...>", "step": <int>, "rank": "desc"|"asc"}. ("most"/
  "highest" ⇒ desc.)
- "stipend_compare": a coaching/extra-duty position's stipend across districts,
  ranked. params: {"position": "<e.g. head football coach>", "rank": "desc"|"asc"}.
- "delta_over_years": change in one lane+step's salary across school years.
  params: {"lane": "<...>", "step": <int>}.

Rules:
- Copy the lane label as the user says it (e.g. "master's +30" → "MA+30"); do not
  invent a step the user didn't give.
- "years 10 and 20" and "step 10 and 20" both map to step_from/step_to (10, 20).
- If it doesn't clearly fit a shape (open-ended, about policies/minutes/events,
  or needs data we don't structure like budgets), return semantic.\
"""


@dataclass(frozen=True)
class RouterDecision:
    mode: str                 # 'analytical' | 'semantic'
    query: str | None
    params: dict
    raw: dict = field(default_factory=dict)


async def route(question: str, *, api_key: str, model: str = ROUTER_MODEL) -> RouterDecision:
    """Classify a question. Falls back to semantic on any doubt."""
    import contextlib

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    try:
        resp = await client.messages.create(
            model=model, max_tokens=400, system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    data = parse_model_json(text) or {}
    query = data.get("query")
    mode = data.get("mode")
    if mode != "analytical" or query not in SUPPORTED_QUERIES:
        return RouterDecision("semantic", None, {}, data if isinstance(data, dict) else {})
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return RouterDecision("analytical", query, params, data)


# ---- query results -----------------------------------------------------

class UnsupportedQuery(RuntimeError):
    """The routed query can't be built from the given params — say so, don't guess."""


@dataclass
class AnalyticalResult:
    question: str
    headline: str                 # what was computed, in words
    metric_label: str             # column label for the ranked value
    rows: list[dict]              # ranked; each carries figures + provenance
    caveats: list[str] = field(default_factory=list)
    not_available: list[str] = field(default_factory=list)
    noncomparable: list[dict] = field(default_factory=list)


def _order(rank: object) -> str:
    return "asc" if str(rank).lower() == "asc" else "desc"


def _req_int(p: dict, key: str) -> int:
    v = p.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise UnsupportedQuery(f"missing/invalid '{key}'") from None


def _req_lane(p: dict) -> str:
    raw = str(p.get("lane") or "").strip()
    if not raw:
        raise UnsupportedQuery("missing 'lane'")
    return normalize_lane(raw)


def _all_slugs(cur) -> list[str]:
    cur.execute("select slug from districts order by slug")
    return [r[0] for r in cur.fetchall()]


def _missing(all_slugs: list[str], present: set[str]) -> list[str]:
    return [s for s in all_slugs if s not in present]


_STEP_CAVEAT = (
    "Figures use the printed salary step as the year-of-service axis; for most "
    "schedules the step is the year of service, but where a contract states them "
    "separately these may differ."
)


STEP_SLOPE_SQL = """
with pairs as (
  select district_id, school_year,
    max(salary)      filter (where step = %(a)s) as sal_from,
    max(salary)      filter (where step = %(b)s) as sal_to,
    max(document_id) filter (where step = %(b)s) as doc_id,
    max(page)        filter (where step = %(b)s) as page
  from salary_schedule
  where bargaining_unit = 'teacher' and lane = %(lane)s and step in (%(a)s, %(b)s)
  group by district_id, school_year
),
both as (select * from pairs where sal_from is not null and sal_to is not null),
latest as (
  select distinct on (district_id) district_id, school_year, sal_from, sal_to, doc_id, page
  from both order by district_id, school_year desc
)
select di.slug, l.school_year, l.sal_from, l.sal_to, (l.sal_to - l.sal_from) as delta,
       l.page, d.title, d.source_url
from latest l
join districts di on di.id = l.district_id
join documents d on d.id = l.doc_id
order by delta {order}
"""


def _step_slope(cur, p: dict, all_slugs: list[str]) -> AnalyticalResult:
    lane, a, b = _req_lane(p), _req_int(p, "step_from"), _req_int(p, "step_to")
    cur.execute(STEP_SLOPE_SQL.format(order=_order(p.get("rank", "desc"))),
                {"lane": lane, "a": a, "b": b})
    rows = [
        {"slug": r[0], "school_year": r[1], "value": float(r[4]),
         "detail": f"step {a} ${float(r[2]):,.0f} → step {b} ${float(r[3]):,.0f}",
         "page": r[5], "doc_title": r[6], "source_url": r[7]}
        for r in cur.fetchall()
    ]
    return AnalyticalResult(
        question="", headline=f"{lane} salary increase from step {a} to step {b}",
        metric_label="increase", rows=rows,
        caveats=[_STEP_CAVEAT], not_available=_missing(all_slugs, {r["slug"] for r in rows}),
    )


MAX_AT_STEP_SQL = """
with rows as (
  select distinct on (district_id) district_id, school_year, salary, document_id, page
  from salary_schedule
  where bargaining_unit = 'teacher' and lane = %(lane)s and step = %(step)s
  order by district_id, school_year desc
)
select di.slug, r.school_year, r.salary, r.page, d.title, d.source_url
from rows r
join districts di on di.id = r.district_id
join documents d on d.id = r.document_id
order by r.salary {order}
"""


def _max_at_step(cur, p: dict, all_slugs: list[str]) -> AnalyticalResult:
    lane, step = _req_lane(p), _req_int(p, "step")
    cur.execute(MAX_AT_STEP_SQL.format(order=_order(p.get("rank", "desc"))),
                {"lane": lane, "step": step})
    rows = [
        {"slug": r[0], "school_year": r[1], "value": float(r[2]),
         "detail": f"{lane} step {step}", "page": r[3], "doc_title": r[4],
         "source_url": r[5]}
        for r in cur.fetchall()
    ]
    return AnalyticalResult(
        question="", headline=f"{lane} salary at step {step}", metric_label="salary",
        rows=rows, caveats=[_STEP_CAVEAT],
        not_available=_missing(all_slugs, {r["slug"] for r in rows}),
    )


STIPEND_SQL = """
with rows as (
  select distinct on (district_id) district_id, school_year, position, tier,
         amount, amount_high, document_id, page
  from stipend_schedule
  where position ilike %(pos)s and amount is not null
        and amount_basis in ('flat', 'range')
  order by district_id, school_year desc, amount desc
)
select di.slug, r.school_year, r.position, r.tier, r.amount, r.amount_high, r.page,
       d.title, d.source_url
from rows r
join districts di on di.id = r.district_id
join documents d on d.id = r.document_id
order by r.amount {order}
"""

STIPEND_PCT_SQL = """
select distinct on (di.slug) di.slug, s.position, s.amount_pct, s.school_year
from stipend_schedule s join districts di on di.id = s.district_id
where s.position ilike %(pos)s and s.amount_basis = 'percent_of_base'
      and s.amount_pct is not null
order by di.slug, s.school_year desc
"""


def _stipend_compare(cur, p: dict, all_slugs: list[str]) -> AnalyticalResult:
    pos = str(p.get("position") or "").strip()
    if not pos:
        raise UnsupportedQuery("missing 'position'")
    like = f"%{pos}%"
    cur.execute(STIPEND_SQL.format(order=_order(p.get("rank", "desc"))), {"pos": like})
    rows = [
        {"slug": r[0], "school_year": r[1], "value": float(r[4]),
         "detail": (f"{r[2]}" + (f" ({r[3]})" if r[3] else "")
                    + (f", range to ${float(r[5]):,.0f}" if r[5] else "")),
         "page": r[6], "doc_title": r[7], "source_url": r[8]}
        for r in cur.fetchall()
    ]
    cur.execute(STIPEND_PCT_SQL, {"pos": like})
    noncomp = [{"slug": r[0], "position": r[1], "pct": float(r[2]), "school_year": r[3]}
               for r in cur.fetchall()]
    return AnalyticalResult(
        question="", headline=f"stipend for “{pos}”", metric_label="stipend",
        rows=rows, not_available=_missing(all_slugs, {r["slug"] for r in rows}),
        noncomparable=noncomp,
        caveats=(["Percent-of-base stipends are listed separately — they depend on "
                  "the holder's own salary and aren't comparable to flat amounts."]
                 if noncomp else []),
    )


DELTA_YEARS_SQL = """
with rows as (
  select district_id, school_year, salary, document_id, page
  from salary_schedule
  where bargaining_unit = 'teacher' and lane = %(lane)s and step = %(step)s
),
agg as (
  select district_id,
    (array_agg(salary order by school_year asc))[1]  as sal_first,
    (array_agg(salary order by school_year desc))[1] as sal_last,
    min(school_year) as yr_first, max(school_year) as yr_last,
    (array_agg(document_id order by school_year desc))[1] as doc_id,
    (array_agg(page order by school_year desc))[1] as page,
    count(distinct school_year) as n_years
  from rows group by district_id
)
select di.slug, a.sal_first, a.sal_last, (a.sal_last - a.sal_first) as delta,
       a.yr_first, a.yr_last, a.page, d.title, d.source_url
from agg a
join districts di on di.id = a.district_id
join documents d on d.id = a.doc_id
where a.n_years >= 2
order by delta desc
"""


def _delta_over_years(cur, p: dict, all_slugs: list[str]) -> AnalyticalResult:
    lane, step = _req_lane(p), _req_int(p, "step")
    cur.execute(DELTA_YEARS_SQL, {"lane": lane, "step": step})
    rows = [
        {"slug": r[0], "school_year": f"{r[4]}→{r[5]}", "value": float(r[3]),
         "detail": f"${float(r[1]):,.0f} → ${float(r[2]):,.0f}", "page": r[6],
         "doc_title": r[7], "source_url": r[8]}
        for r in cur.fetchall()
    ]
    return AnalyticalResult(
        question="", headline=f"{lane} step {step} salary change across school years",
        metric_label="change", rows=rows, caveats=[_STEP_CAVEAT],
        not_available=_missing(all_slugs, {r["slug"] for r in rows}),
    )


_RUNNERS = {
    "step_slope": _step_slope,
    "max_at_step": _max_at_step,
    "stipend_compare": _stipend_compare,
    "delta_over_years": _delta_over_years,
}


def run_query(cur, decision: RouterDecision) -> AnalyticalResult:
    """Execute the routed template. Raises UnsupportedQuery if params don't fit."""
    if decision.query not in _RUNNERS:
        raise UnsupportedQuery(f"unsupported query '{decision.query}'")
    all_slugs = _all_slugs(cur)
    return _RUNNERS[decision.query](cur, decision.params, all_slugs)


# ---- rendering ---------------------------------------------------------

def render_markdown(result: AnalyticalResult) -> str:
    """A cited, ranked answer. Numbers are SQL-computed, not model-written."""
    lines = [f"# {result.question}", ""]
    if not result.rows:
        lines += [
            f"No extracted {result.metric_label} data matches this "
            "question yet — the relevant schedules may not be extracted (or not "
            "in the corpus).",
            "",
        ]
    else:
        lines += [f"**{result.headline}** — ranked across districts:", ""]
        for i, r in enumerate(result.rows, 1):
            lines.append(
                f"{i}. **{r['slug']}** — ${r['value']:,.0f} "
                f"({r['detail']}, {r['school_year']}) [{i}]"
            )
        lines.append("")
    for c in result.caveats:
        lines.append(f"_{c}_")
    if result.noncomparable:
        lines.append("")
        lines.append("**Percent-of-base (not directly comparable):**")
        for nc in result.noncomparable:
            lines.append(f"- {nc['slug']}: {nc['pct']:g}% of base — {nc['position']}")
    if result.not_available:
        lines.append("")
        lines.append(
            "_No extracted schedule for this query in: "
            + ", ".join(result.not_available) + "._"
        )
    if result.rows:
        lines += ["", "## Sources", ""]
        for i, r in enumerate(result.rows, 1):
            page = f" · p.{r['page']}" if r.get("page") else ""
            lines.append(
                f"**[{i}]** {r['slug']} · {r['school_year']} · {r['doc_title']}{page}  \n"
                f"<{r['source_url']}>"
            )
            lines.append("")
    lines.append("_Answer computed from the extracted salary/stipend tables; "
                 "figures are quoted from the cited source documents._")
    return "\n".join(lines) + "\n"
