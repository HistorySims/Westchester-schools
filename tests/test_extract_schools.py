"""Tests for structured extraction (herald-extract) and its taxonomy."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from uuid import UUID

from herald import taxonomy
from herald.extract_schools import (
    AuditViolation,
    Candidate,
    _bool,
    _candidate_sql,
    _num,
    _school_year_from_date,
    audit_salary,
    build_salary_rows,
    build_stipend_rows,
    extract_candidates,
    parse_model_json,
    render_report,
)
from herald.schools_db import (
    SalaryScheduleRow,
    StipendScheduleRow,
    mark_chunk_extracted,
    upsert_salary,
    upsert_stipend,
)

DID = UUID("11111111-1111-1111-1111-111111111111")
DOCID = UUID("22222222-2222-2222-2222-222222222222")
CID = UUID("33333333-3333-3333-3333-333333333333")


# ---- taxonomy ----------------------------------------------------------

def test_normalize_lane_patterns():
    assert taxonomy.normalize_lane("MA+30") == "MA+30"
    assert taxonomy.normalize_lane("M+30") == "MA+30"
    assert taxonomy.normalize_lane("Master's plus 30") == "MA+30"
    assert taxonomy.normalize_lane("BA") == "BA"
    assert taxonomy.normalize_lane("Bachelors") == "BA"
    assert taxonomy.normalize_lane("Doctorate") == "Doctorate"
    assert taxonomy.normalize_lane("Ph.D.") == "Doctorate"
    assert taxonomy.normalize_lane("Column V") == "other"   # opaque -> other
    assert taxonomy.normalize_lane("") == "other"


def test_normalize_lane_crosswalk_wins(tmp_path):
    csv = tmp_path / "cw.csv"
    csv.write_text(
        "# comment line\n"
        "district_slug,lane_raw,lane_canonical\n"
        "ossining,Column V,MA+30\n"
        ",Level 4,MA+15\n",
        encoding="utf-8",
    )
    cw = taxonomy.load_crosswalk(csv)
    # district-specific mapping
    assert taxonomy.normalize_lane("Column V", district_slug="ossining", crosswalk=cw) == "MA+30"
    # global mapping (blank district)
    assert taxonomy.normalize_lane("Level 4", district_slug="peekskill", crosswalk=cw) == "MA+15"
    # a district without the mapping still falls back to pattern rules
    assert taxonomy.normalize_lane("Column V", district_slug="peekskill", crosswalk=cw) == "other"


def test_lane_rank_orders_canonical():
    assert taxonomy.lane_rank("BA") < taxonomy.lane_rank("MA")
    assert taxonomy.lane_rank("MA") < taxonomy.lane_rank("MA+30")
    assert taxonomy.lane_rank("other") == -1


def test_infer_category():
    assert taxonomy.infer_category("Head Football Coach") == "athletics"
    assert taxonomy.infer_category("Yearbook Advisor") == "cocurricular"
    assert taxonomy.infer_category("Detention Monitor") == "extra_duty"


# ---- parsing / coercion ------------------------------------------------

def test_parse_model_json_tolerates_fences_and_prose():
    assert parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_model_json('Sure!\n{"a": 2}\nDone') == {"a": 2}
    assert parse_model_json("not json at all") is None


def test_num_and_bool_coercion():
    assert _num("87,432") == 87432.0
    assert _num("$1,234.50") == 1234.5
    assert _num("") is None
    assert _num(None) is None
    assert _num(8500) == 8500.0
    assert _bool("true") is True
    assert _bool("no") is False
    assert _bool(True) is True


def test_school_year_from_date():
    assert _school_year_from_date(_dt.date(2026, 3, 1)) == "2025-26"
    assert _school_year_from_date(_dt.date(2026, 9, 1)) == "2026-27"
    assert _school_year_from_date(None) is None


def test_school_year_from_title():
    from herald.extract_schools import _school_year_from_title

    # a multi-year contract term -> its start school year
    assert _school_year_from_title("PFA Agreement 2023-2026") == "2023-24"
    # a single school year, as printed
    assert _school_year_from_title("2024-25 Salary Schedule") == "2024-25"
    assert _school_year_from_title("MVAG Contract and MOA") is None


def test_build_salary_rows_uses_title_year_when_grid_and_date_lack_one():
    # The teacher-CBA case that produced zero rows: model returns null year, no
    # meeting_date, but the title's year (passed as fallback) rescues the rows.
    data = {"salary_rows": [
        {"lane_raw": "MA+30", "step": 5, "salary": 82000},   # no school_year
    ]}
    rows, skipped = build_salary_rows(
        data, district_slug="peekskill", crosswalk={}, page=8, fallback_year="2023-24",
    )
    assert skipped == 0
    assert rows[0].school_year == "2023-24" and rows[0].salary == 82000.0
    assert rows[0].notes and "inferred" in rows[0].notes


# ---- row building ------------------------------------------------------

def test_build_salary_rows_normalizes_and_infers_year():
    data = {"salary_rows": [
        {"school_year": "2024-25", "lane_raw": "MA+30", "step": 5, "salary": "72,000"},
        {"school_year": None, "lane_raw": "BA", "step": 1, "salary": 55000},   # infer year
        {"lane_raw": "MA", "salary": 60000},                                    # no step -> skip
    ]}
    rows, skipped = build_salary_rows(
        data, district_slug="ossining", crosswalk={}, page=4, fallback_year="2025-26",
    )
    assert skipped == 1
    assert [r.lane for r in rows] == ["MA+30", "BA"]
    assert rows[0].school_year == "2024-25" and rows[0].salary == 72000.0
    assert rows[0].page == 4
    assert rows[1].school_year == "2025-26"  # inferred
    assert rows[1].notes and "inferred" in rows[1].notes


def test_build_stipend_rows_basis_and_category():
    data = {"stipend_rows": [
        {"position_raw": "Football - Head", "position": "Head Football Coach",
         "amount": "8,500"},                                        # flat inferred
        {"position_raw": "Dept Chair", "amount_pct": 3.5},          # percent_of_base
        {"position_raw": "blank"},                                  # no amount -> skip
    ]}
    rows, skipped = build_stipend_rows(data, page=2, fallback_year="2025-26")
    assert skipped == 1
    assert rows[0].amount == 8500.0 and rows[0].amount_basis == "flat"
    assert rows[0].category == "athletics"
    assert rows[1].amount_basis == "percent_of_base" and rows[1].amount_pct == 3.5


# ---- audit -------------------------------------------------------------

def _sr(**kw):
    base = dict(school_year="2024-25", lane="MA+30", lane_raw="MA+30", step=1,
                years_service=None, is_longevity=False, salary=60000.0)
    base.update(kw)
    return SalaryScheduleRow(**base)


def test_audit_flags_non_monotonic_step():
    rows = [("ossining", _sr(step=1, salary=60000)),
            ("ossining", _sr(step=2, salary=58000))]   # drops
    kinds = {v.kind for v in audit_salary(rows)}
    assert "salary_non_monotonic" in kinds


def test_audit_flags_lane_out_of_order_and_bounds():
    rows = [("x", _sr(lane="MA", step=5, salary=90000)),
            ("x", _sr(lane="MA+30", step=5, salary=88000)),   # higher lane pays less
            ("x", _sr(lane="BA", step=1, salary=5000))]       # below the sanity band
    kinds = {v.kind for v in audit_salary(rows)}
    assert "lane_out_of_order" in kinds
    assert "salary_out_of_bounds" in kinds


def test_audit_flags_year_over_year_drop():
    rows = [("x", _sr(school_year="2023-24", salary=61000)),
            ("x", _sr(school_year="2024-25", salary=60000))]   # raise expected, dropped
    assert any(v.kind == "year_over_year_drop" for v in audit_salary(rows))


def test_audit_clean_schedule_has_no_violations():
    rows = [("x", _sr(step=1, salary=60000)), ("x", _sr(step=2, salary=62000))]
    assert audit_salary(rows) == []


# ---- candidate SQL -----------------------------------------------------

def test_candidate_sql_shape():
    base = _candidate_sql(district=False, limit=False)
    assert "c.kind = 'table'" in base
    assert "c.extracted_at is null" in base
    assert "%(kw)s" in base
    assert "%(district)s" not in base and "limit" not in base.lower()
    full = _candidate_sql(district=True, limit=True)
    assert "di.slug = %(district)s" in full
    assert full.strip().endswith("limit %(limit)s")
    # --reextract drops the extracted_at guard so processed chunks are re-run
    assert "c.extracted_at is null" not in _candidate_sql(
        district=False, limit=False, only_new=False
    )


# ---- upsert SQL shapes -------------------------------------------------

class _RecCursor:
    def __init__(self):
        self.many: list = []
        self.exec: list = []

    def executemany(self, sql, params):
        self.many.append((" ".join(sql.split()), list(params)))

    def execute(self, sql, params=None):
        self.exec.append((" ".join(sql.split()), params))


def test_upsert_salary_sql_shape():
    cur = _RecCursor()
    n = upsert_salary(cur, district_id=DID, document_id=DOCID, rows=[_sr()])
    assert n == 1
    sql, rows = cur.many[0]
    assert "insert into salary_schedule" in sql
    assert "on conflict (district_id, school_year, lane, step) do update" in sql
    assert rows[0][0] == DID


def test_upsert_stipend_sql_shape_and_mark():
    cur = _RecCursor()
    row = StipendScheduleRow(position="Head Coach", position_raw="Head Coach",
                             school_year="2024-25", amount=5000.0, amount_basis="flat")
    n = upsert_stipend(cur, district_id=DID, document_id=DOCID, rows=[row])
    assert n == 1
    sql, _ = cur.many[0]
    assert "insert into stipend_schedule" in sql
    assert "on conflict (district_id, school_year, position, tier) do update" in sql
    mark_chunk_extracted(cur, CID)
    assert "update chunks set extracted_at = now()" in cur.exec[0][0]


# ---- end-to-end extraction (mocked Claude) -----------------------------

class _FakeResp:
    def __init__(self, text: str):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()


def _patch_anthropic(monkeypatch, responses: list[str]):
    import anthropic

    state = {"i": 0}

    class _Msgs:
        async def create(self, **kw):
            t = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            return _FakeResp(t)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Msgs()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)


def _cand(slug="ossining", content="| lane | step | salary |"):
    return Candidate(chunk_id=CID, document_id=DOCID, district_id=DID, slug=slug,
                     content=content, section_path="T4#1", heading="Table (p. 4)",
                     doc_title="CBA", meeting_date=_dt.date(2025, 6, 1))


def test_extract_candidates_dry_run_classifies_and_audits(monkeypatch):
    salary = json.dumps({"table_kind": "salary", "salary_rows": [
        {"school_year": "2024-25", "lane_raw": "MA+30", "step": 1, "salary": 60000},
        {"school_year": "2024-25", "lane_raw": "MA+30", "step": 2, "salary": 58000},
    ], "stipend_rows": []})
    stipend = json.dumps({"table_kind": "stipend", "salary_rows": [], "stipend_rows": [
        {"position_raw": "Head Football Coach", "position": "Head Football Coach",
         "amount": "8,500", "amount_basis": "flat"},
    ]})
    none = json.dumps({"table_kind": "none", "salary_rows": [], "stipend_rows": []})
    _patch_anthropic(monkeypatch, [salary, stipend, none])

    stats = asyncio.run(extract_candidates(
        None, [_cand(), _cand(), _cand()], api_key="k", model="m",
        dry_run=True, crosswalk={},
    ))
    assert stats.seen == 3
    assert stats.salary_tables == 1 and stats.stipend_tables == 1 and stats.none_tables == 1
    assert stats.salary_rows == 2 and stats.stipend_rows == 1
    # the deliberate step-2 drop is flagged
    assert any(v.kind == "salary_non_monotonic" for v in stats.salary_violations)
    md = render_report(stats, dry_run=True)
    assert "Structured extraction report" in md and "salary_non_monotonic" in md


class _FakeConn:
    def __init__(self):
        self.many: list = []
        self.exec: list = []

    def cursor(self):
        conn = self

        class _C:
            def executemany(self, sql, params):
                conn.many.append((" ".join(sql.split()), list(params)))

            def execute(self, sql, params=None):
                conn.exec.append((" ".join(sql.split()), params))

        return _C()

    def transaction(self):
        from contextlib import nullcontext
        return nullcontext()


def test_extract_candidates_write_upserts_and_marks(monkeypatch):
    salary = json.dumps({"table_kind": "salary", "salary_rows": [
        {"school_year": "2024-25", "lane_raw": "MA", "step": 1, "salary": 60000},
    ], "stipend_rows": []})
    _patch_anthropic(monkeypatch, [salary])
    conn = _FakeConn()

    stats = asyncio.run(extract_candidates(
        conn, [_cand()], api_key="k", model="m", dry_run=False, crosswalk={},
    ))
    assert stats.salary_rows == 1
    assert any("insert into salary_schedule" in sql for sql, _ in conn.many)
    assert any("update chunks set extracted_at" in sql for sql, _ in conn.exec)


def test_render_report_clean_audit(monkeypatch):
    none = json.dumps({"table_kind": "none", "salary_rows": [], "stipend_rows": []})
    _patch_anthropic(monkeypatch, [none])
    stats = asyncio.run(extract_candidates(
        None, [_cand()], api_key="k", model="m", dry_run=True, crosswalk={},
    ))
    assert render_report(stats, dry_run=True).count("No invariant violations") == 1
    _ = AuditViolation  # imported symbol used
