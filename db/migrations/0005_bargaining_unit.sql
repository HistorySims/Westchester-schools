-- ============================================================
-- Bargaining unit dimension for salary/stipend schedules.
--
-- A district runs on more than its teachers — custodial, administrator, aide,
-- clerical, nurse and other units all have negotiated pay schedules that are
-- part of governing the district. Those are kept, not discarded, so each row
-- is tagged with its bargaining unit. Without this, a custodial grid and a
-- teacher grid for the same (year, lane, step) collide on the old unique key
-- and one silently overwrites the other.
--
-- 'teacher' is the default so existing rows (teacher-only so far) are labeled
-- correctly and re-runs stay idempotent.
--
-- Apply:
--   psql "$SUPABASE_DB_URL" -f db/migrations/0005_bargaining_unit.sql
-- ============================================================

-- salary_schedule ------------------------------------------------------------
alter table salary_schedule
  add column if not exists bargaining_unit text not null default 'teacher';

-- Widen the uniqueness/idempotency key to include the unit.
alter table salary_schedule
  drop constraint if exists salary_schedule_district_id_school_year_lane_step_key;
-- Guarded like 0002's check constraint. `add constraint ... unique` builds an
-- index of the same name and has no IF NOT EXISTS, so re-running raised
-- DuplicateTable — which is exactly what happened: this file was applied by
-- hand before schema_migrations existed, so the runner had no record of it and
-- tried again.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'salary_schedule_unit_key'
  ) then
    alter table salary_schedule
      add constraint salary_schedule_unit_key
      unique (district_id, bargaining_unit, school_year, lane, step);
  end if;
end $$;

drop index if exists salary_schedule_lookup_idx;
create index if not exists salary_schedule_lookup_idx
  on salary_schedule (district_id, bargaining_unit, lane, step);

-- stipend_schedule -----------------------------------------------------------
-- Extra-duty / differential pay exists for non-teacher units too; tag it the
-- same way so a custodial differential can't collide with a coaching stipend.
alter table stipend_schedule
  add column if not exists bargaining_unit text not null default 'teacher';

alter table stipend_schedule
  drop constraint if exists stipend_schedule_district_id_school_year_position_tier_key;
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'stipend_schedule_unit_key'
  ) then
    alter table stipend_schedule
      add constraint stipend_schedule_unit_key
      unique (district_id, bargaining_unit, school_year, position, tier);
  end if;
end $$;
