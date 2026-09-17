-- ============================================================
-- Pay basis dimension for salary_schedule.
--
-- Sibling of 0005, for the same class of bug one dimension over. 0005 found
-- that a custodial grid and a teacher grid collide on (year, lane, step); this
-- is the discovery that a district publishes SEVERAL schedules for the SAME
-- unit, on the same lane/step axes, in different units of pay:
--
--   White Plains "2022-2026 Salary Schedule"              BA step 1 = $61,713/year
--   White Plains "2022-2026 Summer School Salary Schedule" BA step 1 = $66/hour
--
-- Both are teacher rows for (2023-24, BA, 1). Under 0005's key one silently
-- overwrote the other, and measured 2026-09-13 the hourly rate won: the corpus
-- held $66 as a White Plains teacher's salary. Tarrytown showed the same shape
-- with longevity INCREMENTS ($2,700 at step 18) stored as if they were
-- salaries.
--
-- The values are all real and worth keeping — a summer-school hourly rate is
-- exactly the sort of thing this corpus should answer about. What they are not
-- is interchangeable, so the basis joins the key and the analytical queries
-- filter to 'annual'.
--
-- 'annual' is the default so existing rows stay put and re-runs stay
-- idempotent. NOTE: that default labels the already-loaded summer-school rows
-- 'annual' too — it cannot know better. Re-extract the affected districts after
-- applying this (docs/STRUCTURED.md, "Pay basis").
--
-- Apply:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0008_pay_basis.sql
-- ============================================================

alter table salary_schedule
  add column if not exists pay_basis text not null default 'annual';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'salary_schedule_pay_basis_chk'
  ) then
    alter table salary_schedule
      add constraint salary_schedule_pay_basis_chk check (pay_basis in
        ('annual', 'hourly', 'daily', 'per_session', 'increment'));
  end if;
end $$;

-- Widen the uniqueness/idempotency key to include the basis. Guarded exactly as
-- 0005 is: `add constraint ... unique` builds an index of the same name and has
-- no IF NOT EXISTS, so an unguarded re-run raises DuplicateTable.
alter table salary_schedule
  drop constraint if exists salary_schedule_unit_key;
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'salary_schedule_basis_key'
  ) then
    alter table salary_schedule
      add constraint salary_schedule_basis_key
      unique (district_id, bargaining_unit, pay_basis, school_year, lane, step);
  end if;
end $$;

-- The analytical path filters on pay_basis, so it leads the lookup index.
drop index if exists salary_schedule_lookup_idx;
create index if not exists salary_schedule_lookup_idx
  on salary_schedule (district_id, bargaining_unit, pay_basis, lane, step);
