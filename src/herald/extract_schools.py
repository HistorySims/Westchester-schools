"""herald-extract: LLM-assisted structured extraction of salary + stipend
schedules from whole-table chunks (docs/STRUCTURED.md §3).

The bounded, auditable approach for ~8 districts: feed each ``kind='table'``
chunk that looks like a schedule to Claude, get back JSON rows against a fixed
schema, normalize lanes/positions *deterministically* here (taxonomy.py, not the
model), and upsert into ``salary_schedule`` / ``stipend_schedule`` with
provenance. A ``--dry-run`` runs the audit invariants (monotonic salary, lane
ordering, year-over-year non-decreasing, sanity bounds) and flags the exact
cells to review before anything is written.

Idempotent: upserts key on (district, year, lane, step) / (district, year,
position, tier), and each processed chunk is stamped ``extracted_at`` so re-runs
skip it. A parse/API failure leaves the chunk unstamped to retry later.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from herald.schools_db import SalaryScheduleRow, StipendScheduleRow
from herald.taxonomy import (
    infer_category,
    lane_rank,
    load_crosswalk,
    normalize_lane,
    normalize_position,
)

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 24000   # a big salary grid is hundreds of JSON rows
MAX_TABLE_CHARS = 20000          # cap the table sent to the model
DEFAULT_CROSSWALK = "data/lane_crosswalk.csv"
SALARY_MIN, SALARY_MAX = 30_000, 250_000   # sanity band for the audit

# Candidate table chunks: headers/content/title mentioning a schedule. POSIX
# ERE (Postgres ~*); '+' is bracketed so it's literal.
CANDIDATE_KEYWORDS = r"salary|stipend|longevity|coach|extra.?duty|co.?curricular|ma[+]|ba[+]"

_VALID_BASIS = {"flat", "range", "percent_of_base"}


EXTRACT_SYSTEM = """\
You extract structured data from a SINGLE table taken from a school-district \
document. First decide the table's kind:
- "salary": a TEACHER SALARY SCHEDULE — a grid of salaries by education lane \
(BA, BA+30, MA, MA+30, Doctorate, "Column V", …) and step / year of service.
- "stipend": a STIPEND / EXTRA-PAY SCHEDULE — pay for coaching, co-curricular, \
or extra-duty positions.
- "none": anything else (budget lines, rosters, calendars, prose tables).

Output ONLY a JSON object, no prose and no code fences:

{
  "table_kind": "salary" | "stipend" | "none",
  "salary_rows": [
    {"school_year": "2024-25" or null, "lane_raw": "<column header, verbatim>",
     "step": <int>, "years_service": <int or null>, "is_longevity": <bool>,
     "salary": <number>}
  ],
  "stipend_rows": [
    {"school_year": "2024-25" or null, "position_raw": "<as printed>",
     "position": "<cleaned title>", "category": "athletics"|"cocurricular"|"extra_duty",
     "tier": "<tier/level or empty string>", "amount": <number or null>,
     "amount_high": <number or null>, "amount_pct": <number or null>,
     "amount_basis": "flat"|"range"|"percent_of_base"}
  ]
}

Rules:
- table_kind "none" ⇒ both arrays empty.
- Numbers are plain: no "$", no commas, no "%". 87,432 → 87432.
- lane_raw: copy the column header EXACTLY; do NOT normalize it (that happens \
downstream). One salary row per filled (lane, step) cell.
- step: the step/row label as printed. years_service: only when the schedule \
states years of service separately from the step number; else null.
- is_longevity: true for longevity rows ("after 15 years", "15/20/25 longevity").
- school_year: the year that grid applies. If the table has a column per year, \
emit one salary row per (year, lane, step). If the grid does NOT show its own \
year, use the school year from the document title (e.g. a "2023-2026" contract \
term ⇒ the first grid is "2023-24"). Only null if neither the table nor the \
title gives a year.
- Stipend amount_basis: "flat" = one dollar figure (in amount); "range" = a \
low-high band (low in amount, high in amount_high); "percent_of_base" = a percent \
of a base salary (percent in amount_pct, amount null).
- Include only cells you can read confidently. Skip totals, subtotals, blank \
cells, and header rows.\
"""


# ---- candidate selection ----------------------------------------------

def _candidate_sql(*, district: bool, limit: bool, only_new: bool = True) -> str:
    where = [
        "c.kind = 'table'",
        "c.status = 'active'",
        "(c.content ~* %(kw)s or c.heading ~* %(kw)s or d.title ~* %(kw)s)",
    ]
    if only_new:
        where.append("c.extracted_at is null")
    if district:
        where.append("di.slug = %(district)s")
    sql = (
        "select c.id, c.document_id, d.district_id, di.slug, c.content, "
        "c.section_path, c.heading, d.title, d.meeting_date "
        "from chunks c "
        "join documents d on d.id = c.document_id "
        "join districts di on di.id = d.district_id "
        f"where {' and '.join(where)} "
        "order by di.slug, d.meeting_date desc nulls last"
    )
    if limit:
        sql += " limit %(limit)s"
    return sql


@dataclass(frozen=True)
class Candidate:
    chunk_id: object
    document_id: object
    district_id: object
    slug: str
    content: str
    section_path: str
    heading: str | None
    doc_title: str
    meeting_date: _dt.date | None

    @property
    def page(self) -> int | None:
        m = re.match(r"^T(\d+)", self.section_path or "")
        return int(m.group(1)) if m else None


# ---- parsing / coercion -----------------------------------------------

def parse_model_json(text: str) -> dict | None:
    """Parse the model's JSON, tolerating code fences and surrounding prose."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        obj = json.loads(t[i : j + 1])
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _num(x: object) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, int | float):
        return float(x)
    s = str(x).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "-", "—", "–"):  # noqa: RUF001  (real em/en-dash placeholders in PDFs)
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group()) if m else None


def _int(x: object) -> int | None:
    n = _num(x)
    return int(n) if n is not None else None


def _bool(x: object) -> bool:
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ("true", "yes", "y", "1")


def _school_year_from_date(d: _dt.date | None) -> str | None:
    """A school year like '2025-26' straddling a meeting date (July rollover)."""
    if d is None:
        return None
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


_TITLE_YEAR = re.compile(r"(20\d{2})\s*[-–—/]\s*(20\d{2}|\d{2})")  # noqa: RUF001


def _school_year_from_title(title: str) -> str | None:
    """First school year in a title: '2024-25' → '2024-25'; a multi-year contract
    term like 'PFA Agreement 2023-2026' → its start school year '2023-24'."""
    m = _TITLE_YEAR.search(title or "")
    if not m:
        return None
    start = int(m.group(1))
    return f"{start}-{(start + 1) % 100:02d}"


def build_salary_rows(
    data: dict, *, district_slug: str, crosswalk, page: int | None,
    fallback_year: str | None,
) -> tuple[list[SalaryScheduleRow], int]:
    rows: list[SalaryScheduleRow] = []
    skipped = 0
    for r in data.get("salary_rows") or []:
        if not isinstance(r, dict):
            skipped += 1
            continue
        salary = _num(r.get("salary"))
        step = _int(r.get("step"))
        lane_raw = str(r.get("lane_raw") or "").strip()
        sy_read = str(r.get("school_year") or "").strip()
        sy = sy_read or (fallback_year or "")
        if salary is None or step is None or not lane_raw or not sy:
            skipped += 1
            continue
        rows.append(SalaryScheduleRow(
            school_year=sy,
            lane=normalize_lane(lane_raw, district_slug=district_slug, crosswalk=crosswalk),
            lane_raw=lane_raw,
            step=step,
            years_service=_int(r.get("years_service")),
            is_longevity=_bool(r.get("is_longevity")),
            salary=salary,
            page=page,
            notes=None if sy_read else "school_year inferred from document title/date",
        ))
    return rows, skipped


def build_stipend_rows(
    data: dict, *, page: int | None, fallback_year: str | None,
) -> tuple[list[StipendScheduleRow], int]:
    rows: list[StipendScheduleRow] = []
    skipped = 0
    for r in data.get("stipend_rows") or []:
        if not isinstance(r, dict):
            skipped += 1
            continue
        position_raw = str(r.get("position_raw") or r.get("position") or "").strip()
        amount = _num(r.get("amount"))
        amount_high = _num(r.get("amount_high"))
        amount_pct = _num(r.get("amount_pct"))
        if not position_raw or (amount is None and amount_high is None and amount_pct is None):
            skipped += 1
            continue
        basis = str(r.get("amount_basis") or "").strip().lower()
        if basis not in _VALID_BASIS:
            basis = ("percent_of_base" if amount_pct is not None
                     else "range" if amount_high is not None else "flat")
        category = str(r.get("category") or "").strip().lower()
        if category not in ("athletics", "cocurricular", "extra_duty"):
            category = infer_category(position_raw)
        sy = str(r.get("school_year") or "").strip() or (fallback_year or "")
        rows.append(StipendScheduleRow(
            position=normalize_position(str(r.get("position") or position_raw)),
            position_raw=position_raw,
            school_year=sy,
            category=category,
            tier=str(r.get("tier") or "").strip(),
            amount=amount,
            amount_high=amount_high,
            amount_pct=amount_pct,
            amount_basis=basis,
            page=page,
        ))
    return rows, skipped


# ---- audit invariants (pure) ------------------------------------------

@dataclass(frozen=True)
class AuditViolation:
    kind: str
    district: str
    detail: str


def audit_salary(rows: list[tuple[str, SalaryScheduleRow]]) -> list[AuditViolation]:
    """Flag likely misreads: non-monotonic steps, out-of-order lanes, YoY drops,
    out-of-band salaries. Advisory — a violation is a cell to review, not a reject."""
    v: list[AuditViolation] = []

    by_lane: dict[tuple, list[SalaryScheduleRow]] = defaultdict(list)
    by_step: dict[tuple, list[SalaryScheduleRow]] = defaultdict(list)
    by_cell: dict[tuple, list[SalaryScheduleRow]] = defaultdict(list)
    for slug, r in rows:
        by_lane[(slug, r.school_year, r.lane)].append(r)
        if lane_rank(r.lane) >= 0:
            by_step[(slug, r.school_year, r.step)].append(r)
        by_cell[(slug, r.lane, r.step)].append(r)

    for (slug, sy, lane), rs in by_lane.items():
        for a, b in pairwise(sorted(rs, key=lambda r: r.step)):
            if b.salary < a.salary:
                v.append(AuditViolation("salary_non_monotonic", slug,
                    f"{sy} {lane}: step {a.step} ${a.salary:,.0f} → step {b.step} "
                    f"${b.salary:,.0f} (drops)"))

    for (slug, sy, step), rs in by_step.items():
        for a, b in pairwise(sorted(rs, key=lambda r: lane_rank(r.lane))):
            if b.salary < a.salary:
                v.append(AuditViolation("lane_out_of_order", slug,
                    f"{sy} step {step}: {a.lane} ${a.salary:,.0f} > {b.lane} "
                    f"${b.salary:,.0f}"))

    for (slug, lane, step), rs in by_cell.items():
        for a, b in pairwise(sorted(rs, key=lambda r: r.school_year)):
            if a.school_year != b.school_year and b.salary < a.salary:
                v.append(AuditViolation("year_over_year_drop", slug,
                    f"{lane} step {step}: {a.school_year} ${a.salary:,.0f} → "
                    f"{b.school_year} ${b.salary:,.0f}"))

    for slug, r in rows:
        if r.salary < SALARY_MIN or r.salary > SALARY_MAX:
            v.append(AuditViolation("salary_out_of_bounds", slug,
                f"{r.school_year} {r.lane} step {r.step}: ${r.salary:,.0f}"))
    return v


def audit_stipend(rows: list[tuple[str, StipendScheduleRow]]) -> list[AuditViolation]:
    v: list[AuditViolation] = []
    for slug, r in rows:
        for amt in (r.amount, r.amount_high):
            if amt is not None and amt < 0:
                v.append(AuditViolation("stipend_negative", slug, f"{r.position}: {amt}"))
    return v


# ---- orchestration -----------------------------------------------------

@dataclass
class ExtractStats:
    seen: int = 0
    salary_tables: int = 0
    stipend_tables: int = 0
    none_tables: int = 0
    salary_rows: int = 0
    stipend_rows: int = 0
    skipped_rows: int = 0
    parse_failed: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_district: Counter[str] = field(default_factory=Counter)
    salary_violations: list[AuditViolation] = field(default_factory=list)
    stipend_violations: list[AuditViolation] = field(default_factory=list)


async def _extract_one(client, model: str, cand: Candidate, *, max_tokens: int):
    date_s = cand.meeting_date.isoformat() if cand.meeting_date else "undated"
    user = (
        f"District: {cand.slug}\nDocument: {cand.doc_title}\nDate: {date_s}\n"
        f"Table heading: {cand.heading or ''}\n\n"
        f"Table (markdown):\n{cand.content[:MAX_TABLE_CHARS]}\n\n"
        "Extract per the schema in your instructions. Output ONLY the JSON object."
    )
    # Stream: at max_tokens this large the SDK refuses a non-streaming request
    # ("streaming is required for operations that may take >10 minutes").
    async with client.messages.stream(
        model=model, max_tokens=max_tokens, system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        final = await stream.get_final_message()
    text = "".join(b.text for b in final.content if b.type == "text")
    return text, final.usage.input_tokens, final.usage.output_tokens


async def extract_candidates(
    conn, candidates: list[Candidate], *, api_key: str, model: str,
    dry_run: bool, crosswalk, max_tokens: int = DEFAULT_MAX_TOKENS, on_doc=None,
) -> ExtractStats:
    """Extract every candidate; upsert + stamp on a real run. Audits the full run."""
    from anthropic import AsyncAnthropic

    from herald import schools_db

    client = AsyncAnthropic(api_key=api_key)
    stats = ExtractStats()
    salary_all: list[tuple[str, SalaryScheduleRow]] = []
    stipend_all: list[tuple[str, StipendScheduleRow]] = []
    fallback: dict[object, str | None] = {}

    try:
        for cand in candidates:
            stats.seen += 1
            note = "ok"
            try:
                raw, in_tok, out_tok = await _extract_one(
                    client, model, cand, max_tokens=max_tokens
                )
            except Exception as exc:  # API/network — leave unstamped to retry
                stats.errors += 1
                note = f"error: {exc}"
                logger.warning("extract failed for chunk %s: %s", cand.chunk_id, exc)
                if on_doc:
                    on_doc(cand, note)
                continue
            stats.input_tokens += in_tok
            stats.output_tokens += out_tok

            data = parse_model_json(raw)
            if data is None:
                stats.parse_failed += 1
                if on_doc:
                    on_doc(cand, "parse-failed")
                continue

            # A teacher CBA grid rarely repeats its year and the doc has no
            # meeting_date, so prefer the year in the title ("… 2023-2026") —
            # without it every salary row is dropped for a missing school_year.
            fy = fallback.setdefault(
                cand.chunk_id,
                _school_year_from_title(cand.doc_title)
                or _school_year_from_date(cand.meeting_date),
            )
            srows, s_sk = build_salary_rows(
                data, district_slug=cand.slug, crosswalk=crosswalk, page=cand.page,
                fallback_year=fy,
            )
            prows, p_sk = build_stipend_rows(data, page=cand.page, fallback_year=fy)
            stats.skipped_rows += s_sk + p_sk
            kind = str(data.get("table_kind") or "none")
            if srows:
                stats.salary_tables += 1
            elif prows:
                stats.stipend_tables += 1
            else:
                stats.none_tables += 1
                note = kind if kind == "none" else f"{kind} (no rows)"

            stats.salary_rows += len(srows)
            stats.stipend_rows += len(prows)
            stats.by_district[cand.slug] += len(srows) + len(prows)
            salary_all += [(cand.slug, r) for r in srows]
            stipend_all += [(cand.slug, r) for r in prows]
            if srows or prows:
                note = f"{len(srows)} salary + {len(prows)} stipend row(s)"

            if not dry_run:
                with conn.transaction():
                    cur = conn.cursor()
                    if srows:
                        schools_db.upsert_salary(cur, district_id=cand.district_id,
                                                 document_id=cand.document_id, rows=srows)
                    if prows:
                        schools_db.upsert_stipend(cur, district_id=cand.district_id,
                                                  document_id=cand.document_id, rows=prows)
                    schools_db.mark_chunk_extracted(cur, cand.chunk_id)
            if on_doc:
                on_doc(cand, note)
    finally:
        with contextlib.suppress(Exception):
            await client.close()

    stats.salary_violations = audit_salary(salary_all)
    stats.stipend_violations = audit_stipend(stipend_all)
    return stats


# ---- reporting ---------------------------------------------------------

def render_report(stats: ExtractStats, *, dry_run: bool) -> str:
    mode = "DRY RUN — nothing written" if dry_run else "written to database"
    lines = [
        "# Structured extraction report",
        "",
        f"_{mode}_",
        "",
        "| tables seen | salary | stipend | none | salary rows | stipend rows "
        "| skipped | parse-fail | errors |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {stats.seen} | {stats.salary_tables} | {stats.stipend_tables} "
        f"| {stats.none_tables} | {stats.salary_rows} | {stats.stipend_rows} "
        f"| {stats.skipped_rows} | {stats.parse_failed} | {stats.errors} |",
        "",
        "## Rows by district",
        "",
        "| district | rows |",
        "|---|---|",
    ]
    lines += [f"| {d} | {n} |" for d, n in stats.by_district.most_common()]
    viol = stats.salary_violations + stats.stipend_violations
    lines += ["", f"## Audit — {len(viol)} flag(s) to review", ""]
    if not viol:
        lines.append("_No invariant violations._")
    else:
        lines += ["| kind | district | detail |", "|---|---|---|"]
        lines += [f"| {x.kind} | {x.district} | {x.detail} |" for x in viol]
    return "\n".join(lines) + "\n"


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Extract salary + stipend schedules into structured tables.",
                  no_args_is_help=True)


@app.callback()
def _main() -> None:
    """Group callback so ``run`` stays a named subcommand."""


@app.command()
def run(
    district: str | None = typer.Option(None, help="Only this district slug."),
    limit: int | None = typer.Option(None, help="Stop after N candidate tables."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--write",
        help="Extract + audit + report only; no DB writes.",
    ),
    reextract: bool = typer.Option(
        False, "--reextract",
        help="Re-process chunks already extracted (default: only new ones).",
    ),
    model: str = typer.Option(DEFAULT_MODEL, help="Extraction model."),
    crosswalk: str = typer.Option(DEFAULT_CROSSWALK, help="Lane crosswalk CSV."),
    report: str | None = typer.Option(None, help="Write a markdown report here."),
) -> None:
    """Find schedule-like table chunks, extract structured rows, audit, load."""
    from herald import schools_db

    db_url = os.environ.get("SUPABASE_DB_URL", "")
    if not db_url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is not set.")

    cw = load_crosswalk(crosswalk)
    conn = schools_db.connect(db_url)
    cur = conn.cursor()
    cur.execute(
        _candidate_sql(district=bool(district), limit=bool(limit), only_new=not reextract),
        {"kw": CANDIDATE_KEYWORDS, "district": district, "limit": limit},
    )
    candidates = [
        Candidate(chunk_id=r[0], document_id=r[1], district_id=r[2], slug=r[3],
                  content=r[4] or "", section_path=r[5] or "", heading=r[6],
                  doc_title=r[7] or "", meeting_date=r[8])
        for r in cur.fetchall()
    ]
    conn.commit()
    console.print(f"{len(candidates)} candidate table(s)"
                  + (f" in {district}" if district else ""))

    done = 0

    def on_doc(cand: Candidate, note: str) -> None:
        nonlocal done
        done += 1
        if note not in ("none",) or done % 25 == 0:
            console.print(f"[{done}/{len(candidates)}] {cand.slug} "
                          f"{cand.doc_title[:44]!r} {note}")

    try:
        stats = asyncio.run(extract_candidates(
            conn, candidates, api_key=api_key, model=model, dry_run=dry_run,
            crosswalk=cw, on_doc=on_doc,
        ))
    finally:
        conn.close()

    table = Table(title="Extract" + (" (dry run)" if dry_run else ""))
    for col in ("seen", "salary", "stipend", "none", "salary rows", "stipend rows",
                "errors"):
        table.add_column(col, justify="right")
    table.add_row(str(stats.seen), str(stats.salary_tables), str(stats.stipend_tables),
                  str(stats.none_tables), str(stats.salary_rows), str(stats.stipend_rows),
                  str(stats.errors))
    console.print(table)
    viol = stats.salary_violations + stats.stipend_violations
    console.print(f"[bold]{len(viol)}[/bold] audit flag(s) to review; "
                  f"tokens: {stats.input_tokens:,} in / {stats.output_tokens:,} out")
    for x in viol[:20]:
        console.print(f"  [yellow]{x.kind}[/yellow] {x.district}: {x.detail}")

    if report:
        Path(report).write_text(render_report(stats, dry_run=dry_run), encoding="utf-8")
        console.print(f"report: {report}")


if __name__ == "__main__":
    app()
