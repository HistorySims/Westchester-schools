# Structured data & the analytical query path

*Design doc. Status: proposed. Prereq for answering parametric questions over
tables — teacher salary steps, coach stipends, budgets.*

## The problem

RAG answers **semantic** questions ("what's the cell-phone policy?") well. It
**cannot** answer **parametric/analytical** questions over tables:

> *"Which district has the steepest salary steps between years 10 and 20 for
> teachers with a Master's + 30 credits?"*

Three compounding failures:

1. **Chunking destroys the grid.** A salary schedule is 2-D — rows are
   steps/years, columns are lanes (BA, BA+30, **MA+30**, …). Linearized and
   chunked, a cell `$87,432` loses its coordinates; the headers that give it
   meaning are chunks away.
2. **The question is a computation, not a lookup.** Find the MA+30 column, read
   steps 10 and 20, compute the delta, do it for all 8 districts, rank. That's
   multi-cell arithmetic + cross-document aggregation, which an LLM reasoning
   over scattered fragments gets wrong.
3. **Retrieval misses.** The relevant cells are a wall of numbers; the query
   embedding won't surface them.

This is the wrong *mode*. The fix is to **extract the handful of high-value
tables into structured rows** and answer these questions with a **query**, not
retrieval. Tractable because the corpus is small: ~8 districts × a few
schedules each — a bounded set we can extract carefully and check by hand.

## Architecture

```
ingest ─► table-aware chunking (keep tables whole, kind='table')
                         │
                         ▼
        LLM extraction pass (herald-extract) ─► structured tables in Supabase
                         │                        (salary_schedule, stipend_schedule, …)
                         ▼
Ask  ─► router ─┬─ semantic  ─► panel RAG (today)
                └─ analytical ─► query the structured tables ─► computed, cited answer
```

Three new pieces: **(1)** table-aware chunking in ingest, **(2)** an extraction
pass that fills structured tables, **(3)** a router + analytical path in Ask.

---

## 1. Table-aware chunking (prerequisite)

At ingest, detect tables (PyMuPDF `page.find_tables()`, or a grid/number-density
heuristic) and keep **each table as one chunk** — never split a grid across
chunks. Store it as markdown with headers intact, tagged `kind='table'`. Two
payoffs: retrieval finds a whole schedule, and the extraction pass (below) gets
a clean, header-bearing input. Add a `kind text` column to `chunks`
(`'prose' | 'table'`), default `'prose'`.

Cheap and useful on its own — even before extraction, a single-district table
question can now retrieve the whole grid and let the model read it.

---

## 2. The structured schema (the crux)

Normalized tables in the same Supabase DB (new migration). Every row carries
**provenance** — `document_id` + `page` — so computed answers cite sources like
RAG answers do. Store both the *normalized* value and the *raw* label so
normalization is auditable.

### Teacher salary schedules

A CBA salary grid is `(school_year, lane, step) → salary`. The quirks that the
schema must capture (and where a naïve parser fails):

- **Step ≠ year.** Steps usually track years of service for the first stretch,
  then diverge — frozen steps, **longevity steps** at 15/20/25, off-schedule
  columns. Capture the step number *and* the stated years-of-service when the
  contract gives it.
- **Lanes vary by district** — "MA+30", "M+30", "Master's plus 30", "Column V".
  Normalize to a canonical label; keep the raw.
- **Multi-year contracts** — a CBA covers several school years, each with its
  own full grid (raises). Store every year's grid; questions usually want the
  most recent.

```sql
create table salary_schedule (
  id             uuid primary key default gen_random_uuid(),
  district_id    uuid not null references districts(id),
  document_id    uuid not null references documents(id),
  page           int,
  school_year    text not null,        -- '2024-25' (the year this grid applies)
  lane           text not null,        -- canonical, e.g. 'MA+30'
  lane_raw       text not null,        -- as printed
  step           int not null,         -- step number as printed
  years_service  int,                  -- mapped years, when the contract states it
  is_longevity   boolean default false,
  salary         numeric not null,
  notes          text,
  created_at     timestamptz default now(),
  unique (district_id, school_year, lane, step)
);
```

**Canonical lanes** (define once): `BA, BA+15, BA+30, MA, MA+15, MA+30, MA+45,
MA+60, MA+75, Doctorate`. Anything unmapped → `lane='other'`, keep `lane_raw`.

**Lane crosswalk file (decided).** Some districts label lanes opaquely
("Column V", "Level 4") whose meaning lives elsewhere in the CBA — or nowhere in
the grid. The model can't infer those from the table alone. Keep a small,
hand-maintained per-district crosswalk (`data/lane_crosswalk.csv`:
`district_slug, lane_raw, lane_canonical`) that `herald-extract` consults before
falling back to `lane='other'`. At n=8 districts this is far more auditable than
hoping the model guesses. Same pattern available for opaque stipend tiers if
needed.

### Stipend schedules (coaches, co-curricular, extra-duty)

Directly serves *"which schools pay coaches an unusual amount."*

```sql
create table stipend_schedule (
  id           uuid primary key default gen_random_uuid(),
  district_id  uuid not null references districts(id),
  document_id  uuid not null references documents(id),
  page         int,
  school_year  text,
  category     text,        -- 'athletics' | 'cocurricular' | 'extra_duty'
  position     text not null,   -- canonical, e.g. 'Head Football Coach'
  position_raw text not null,
  tier         text,        -- level/experience tier if the schedule has one
  amount       numeric,     -- flat dollars, or the low end of a range
  amount_high  numeric,     -- high end if a range
  amount_pct   numeric,     -- the percent, when amount_basis = 'percent_of_base'
  amount_basis text,        -- 'flat' | 'range' | 'percent_of_base'
  notes        text,
  unique (district_id, school_year, position, tier)
);
```

**`percent_of_base` convention (decided).** Some districts express a stipend as
a percent of a base salary. The real dollar figure depends on *who holds the
position* (their own salary), which the schedule doesn't fix — so a percent
stipend is **not directly comparable** to a flat-dollar one. Rule: store
`amount_pct` + `amount_basis='percent_of_base'` and **exclude these rows from
flat-dollar rankings**, reporting them separately in the answer ("District X
pays this as N% of base — not directly comparable to flat stipends"). We do
*not* silently invent a dollar amount. (A future opt-in could convert at a
single disclosed reference — the district's own BA step-1 for that year — but
labeled illustrative, never mixed into the ranking.)

### Budgets (phase 2b — messier, defer)

Highest volume and most variable format. Line items
`(fiscal_year, account_code, description, amount, amount_type)`. Worth doing but
after salary + stipend prove the pattern; the account-code structure actually
helps normalization.

---

## 3. The extraction pass (`herald-extract`)

**Bounded and LLM-assisted** — the winning move for ~8 districts. Not a general
robust table parser (brittle); a careful per-artifact extraction we can audit.

1. **Find candidates** — documents/chunks likely to hold a schedule:
   `doc_type='contract'`, or `kind='table'` chunks whose headers match
   `salary schedule | step | lane | stipend | appendix`.
2. **Extract** — feed each whole-table chunk to Claude with the target schema
   and a strict "emit JSON rows, normalize lanes/steps, flag longevity, capture
   school_year, keep raw labels" instruction. Validate against the schema.
3. **Load** — upsert rows into the structured tables with provenance. Idempotent
   on the unique keys, so re-runs correct rather than duplicate.
4. **Audit — automated checks, not just eyeballing.** A wall of numbers hides
   transposition and header-misalignment errors that spot-checks miss, so
   `--dry-run` runs invariants and flags violations loudly before any write:
   - salary **monotonic non-decreasing** as `step` increases within a
     `(district, school_year, lane)`
   - **`MA+30 ≥ MA`** (and each higher lane ≥ the lane left of it) at the same
     step
   - a **later `school_year` grid ≥ the earlier** one, cell for cell (raises,
     not cuts — a drop signals a misread grid or a swapped year header)
   - sanity bounds (salary within a plausible $30k–$250k band; stipend ≥ 0)

   A violation doesn't auto-reject — it surfaces the exact cells for a human to
   confirm (some are real: a genuine one-year freeze, a lane that truly dips).
   But it turns "trust the model on 400 numbers" into "review the 3 it flagged."

Runs in GitHub Actions (`extract.yml`), needs `ANTHROPIC_API_KEY`. Cost is
trivial — a handful of Claude calls.

---

## 4. The router + analytical path (Ask)

Ask gains a **router** that classifies each question:

- **Semantic** ("what does the policy say") → today's per-district panel RAG,
  unchanged.
- **Analytical** ("steepest / highest / how much / which district has the
  most") → the structured path.

Classification: a cheap Claude (Haiku) call that returns
`{mode, dataset, params}` — e.g. `{analytical, salary_schedule, {lane:'MA+30',
step_from:10, step_to:20, metric:'slope', group_by:'district', rank:'desc'}}`.

**Query execution — templated, not text-to-SQL.** That router output *is* a
filled template; Haiku already did the hard part (understanding the question).
The SQL for our handful of question shapes is static, so we hand-write it. Start
with the top shapes:

- **step-slope ranking** — delta (or max step-over-step) in a lane between two
  steps/years, ranked across districts (the salary-step question)
- **max-at-step** — highest/lowest salary at a given lane+step
- **stipend comparison** — a position's stipend across districts, ranked
- **delta-over-years** — change in a cell across school years

Each is a parameterized query filled from `params`. The rows come back and
Claude writes the prose answer **citing the source documents/pages** each row
carries. **Failure mode is "I can't answer that yet," not plausible-but-wrong
SQL** — if a question doesn't map to a template, say so. We add text-to-SQL only
if a genuine long tail of expressible-but-untemplated questions shows up; until
then, wrong-and-confident is the failure we refuse to ship.

**Step vs year (correctness rule).** Analytical queries use `years_service`
when it's populated; otherwise they fall back to `step` **and the prose answer
says so explicitly** ("figures assume step = year of service"). Never silently
treat step as year — across districts that would rank different quantities
against each other. A district whose schedule has neither mapped years nor a
usable step for the requested range is reported as "not comparable on this
metric," not dropped silently.

**Honesty about absence** carries over: if a district's schedule isn't extracted
yet, the answer says so — never implies "$0" or "no steep steps."

### Worked example (the salary-step question end to end)

> *"Which district has the steepest salary steps between years 10 and 20 for
> teachers with a Master's + 30 credits?"*

1. Router → `{analytical, salary_schedule, {lane:'MA+30', from:10, to:20,
   metric:'slope', group_by:'district'}}`.
2. SQL over `salary_schedule` (latest `school_year` per district): for each
   district, take `lane='MA+30'`, `step` 10 and 20, compute
   `(salary@20 − salary@10)` (or max step-over-step delta in that range).
3. Rank districts by that delta.
4. Claude presents the ranking with the exact figures and **cites each
   district's CBA salary schedule (document + page)**. Districts with no
   extracted MA+30 schedule are listed as "not available."

An answer that's exact, ranked, and has receipts — impossible with RAG.

---

## Provenance & trust

This is a school-board-watcher tool: numbers need receipts. Every structured row
stores `document_id` + `page`; every computed answer links back to them, so a
claim like "District X's MA+30 step jumps $6,200 at year 15" is one click from
the CBA page it came from. Same bar as the cited RAG answers.

## Decisions (locked)

1. **Templated queries, not text-to-SQL.** The router output is a filled
   template; SQL for our shapes is static. Failure mode = "unsupported
   question," never plausible-but-wrong SQL. Add text-to-SQL only if a real long
   tail appears.
2. **Salary + stipend first; budgets deferred** (2b).
3. **Every contract year's grid** is stored (feeds year-over-year + the brief).
4. **Taxonomies locked before extraction** — canonical lanes + coach positions.
5. **Lane crosswalk file** (`data/lane_crosswalk.csv`) — hand-maintained
   per-district raw→canonical mapping for opaque lanes.
6. **Step vs year** — queries prefer `years_service`, fall back to `step` with
   an explicit caveat in prose; never silently equate them.
7. **`percent_of_base` stipends** — stored as a percent, excluded from
   flat-dollar rankings, reported separately as non-comparable.
8. **Automated audit invariants** in `--dry-run` (monotonic salary, lane
   ordering, year-over-year non-decreasing, sanity bounds).

## Sequencing

1. **This doc** — schema + flow agreed.
2. **Table-aware chunking** in ingest (`kind='table'`) — also improves retrieval.
3. **`herald-extract`** for salary + stipend schedules → structured tables,
   with a `--dry-run` audit.
4. **Router + analytical path** in Ask (CLI first, then `/api/ask`).
5. **Reflect** on other structures that deserve special treatment (see below).
6. Budgets (2b), then re-cluster once the corpus handling is settled.

## Candidates for later "special treatment" (to revisit after 1–4)

Structures that, like tables, don't fit prose-chunk RAG and may deserve their own
mode: **calendars / meeting schedules**, **vote tallies** (who voted how),
**org charts / personnel rosters & appointments**, **bond/capital project line
items**, **enrollment & demographic tables**, **assessment/achievement score
tables**. Each is a small structured extraction with outsized analytical value.
