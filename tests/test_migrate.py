"""Tests for the migration runner.

The bug this exists to prevent is not a crash — it is a migration that never
runs. ``0005_bargaining_unit.sql`` sat unapplied for weeks because applying it
needed ``psql`` on a laptop and the only available device was a phone. So the
things worth asserting are ordering, idempotence, and the refusal to silently
re-run a file that changed after it was applied.
"""

from __future__ import annotations

from pathlib import Path

from herald.migrate import discover, plan


def _write(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def test_discovery_is_in_filename_order(tmp_path):
    # Written out of order on purpose: apply order is the schema's history,
    # and 0010 must not run before 0002 just because it was created first.
    _write(tmp_path, "0010_later.sql", "select 10;")
    _write(tmp_path, "0002_early.sql", "select 2;")
    _write(tmp_path, "0001_first.sql", "select 1;")
    _write(tmp_path, "notes.md", "not a migration")

    assert [m.name for m in discover(tmp_path)] == [
        "0001_first.sql", "0002_early.sql", "0010_later.sql",
    ]


def test_pending_excludes_what_already_ran(tmp_path):
    _write(tmp_path, "0001_a.sql", "select 1;")
    _write(tmp_path, "0002_b.sql", "select 2;")
    found = discover(tmp_path)
    applied = {found[0].name: found[0].sha256}

    pending, changed = plan(found, applied)
    assert [m.name for m in pending] == ["0002_b.sql"]
    assert changed == []


def test_re_running_with_everything_applied_is_a_no_op(tmp_path):
    _write(tmp_path, "0001_a.sql", "select 1;")
    found = discover(tmp_path)
    pending, changed = plan(found, {m.name: m.sha256 for m in found})
    assert pending == [] and changed == []


def test_a_migration_edited_after_it_ran_is_flagged_not_rerun(tmp_path):
    # Re-executing edited DDL is how a schema drifts away from what the
    # migration files claim it is. Surface the disagreement; never guess.
    p = _write(tmp_path, "0001_a.sql", "alter table t add column x int;")
    original = discover(tmp_path)[0].sha256
    p.write_text("alter table t add column x bigint;", encoding="utf-8")

    pending, changed = plan(discover(tmp_path), {"0001_a.sql": original})
    assert [m.name for m in changed] == ["0001_a.sql"]
    assert pending == []            # emphatically NOT queued to run again


def test_the_real_migrations_directory_is_ordered_and_unique():
    found = discover("db/migrations")
    names = [m.name for m in found]
    assert names == sorted(names)
    assert len(set(names)) == len(names)
    # every file carries a numeric prefix, which is what makes order meaningful
    assert all(m.name[:4].isdigit() for m in found)
    assert "0006_doc_type_presentation_financial.sql" in names


def test_every_migration_guards_its_constraint_and_index_creation():
    # 0005 died live with "relation salary_schedule_unit_key already exists".
    # It had been applied by hand before schema_migrations existed, so the
    # runner had no record and re-ran it. `add constraint` builds an index of
    # the same name and has no IF NOT EXISTS, so it is not re-runnable on its
    # own — it needs a pg_constraint guard, as 0002 already used.
    import re

    # Two ways to make `add constraint` re-runnable, both used here:
    #   * wrap it in `do $$ ... if not exists (select 1 from pg_constraint) $$`
    #     (0002, 0005) — keeps an existing constraint untouched;
    #   * `drop constraint if exists X` first (0006) — recreates it, which is
    #     what a CHECK constraint whose allowed list grows actually needs, since
    #     the guard version would skip the update.
    offenders: list[str] = []
    for m in discover("db/migrations"):
        sql = m.sql
        unguarded = re.sub(r"do \$\$.*?end \$\$;", "", sql, flags=re.S | re.I)
        dropped = {n.lower() for n in
                   re.findall(r"drop constraint if exists\s+(\w+)", sql, flags=re.I)}
        for stmt in re.findall(r"add constraint\s+(\w+)", unguarded, flags=re.I):
            if stmt.lower() not in dropped:
                offenders.append(f"{m.name}: add constraint {stmt}")
        for stmt in re.findall(r"create (?:unique )?index (?!if not exists)(\w+)",
                               unguarded, flags=re.I):
            offenders.append(f"{m.name}: create index {stmt}")
    assert not offenders, (
        "these statements cannot be re-run, so the migration is not idempotent "
        f"and a database that already has the change cannot be caught up: {offenders}"
    )


def test_create_table_and_add_column_are_guarded_too():
    import re

    offenders: list[str] = []
    for m in discover("db/migrations"):
        sql = re.sub(r"do \$\$.*?end \$\$;", "", m.sql, flags=re.S | re.I)
        offenders += [f"{m.name}: create table {t}" for t in
                      re.findall(r"create table (?!if not exists)(\w+)", sql, flags=re.I)]
        offenders += [f"{m.name}: add column {c}" for c in
                      re.findall(r"add column (?!if not exists)(\w+)", sql, flags=re.I)]
    assert not offenders, offenders
