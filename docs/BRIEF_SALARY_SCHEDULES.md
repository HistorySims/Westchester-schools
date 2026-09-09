# Brief: get salary schedules extracted for all eight districts

A task brief for a separate Claude Code session. Delete it once the work lands.

## What this repo is

A semantic-research corpus of Westchester County NY school-district governance
documents — agendas, minutes, policies, contracts — in Supabase Postgres with
pgvector. Two goals: **(A)** search and compare current policies and contracts,
**(B)** a monthly newsletter-style overview of what the boards are doing.

## Your task

Goal A is one district out of eight. Asked *"How much would a teacher make in
their third year with a masters and 0 credits?"*, `herald-ask` answers:

```
1. tarrytowns — $79,571 (MA step 3, 2024-25)

No extracted schedule for this query in: elmsford, greenburgh-central,
mount-vernon, ossining, peekskill, port-chester-rye, white-plains.
```

That is not a retrieval bug. `salary_schedule` genuinely has only Tarrytown,
which was the original test case. Get the other seven populated — or find out
why they can't be, which is a real answer too.

## Start with diagnosis, not code

Three outcomes are possible and they need completely different fixes. These
queries separate them and cost nothing.

**1. What `herald-extract run` would find.** This is its own candidate query
(`_candidate_sql` in `src/herald/extract_schools.py`) with the keyword regex
inlined:

```sql
select di.slug,
       count(*)                                       as candidate_tables,
       count(*) filter (where c.extracted_at is null) as never_tried
from chunks c
join documents d  on d.id  = c.document_id
join districts di on di.id = d.district_id
where c.kind = 'table' and c.status = 'active'
  and (c.content ~* 'salary|stipend|longevity|coach|extra.?duty|co.?curricular|\y(ba|ma)\s*\+?\s*\d{2}\y|\yph\.?\s*d\y|\ydoctorate\y|\ystep\y'
    or c.heading ~* 'salary|stipend|longevity|coach|extra.?duty|co.?curricular|\y(ba|ma)\s*\+?\s*\d{2}\y|\yph\.?\s*d\y|\ydoctorate\y|\ystep\y'
    or d.title   ~* 'salary|stipend|longevity|coach|extra.?duty|co.?curricular|\y(ba|ma)\s*\+?\s*\d{2}\y|\yph\.?\s*d\y|\ydoctorate\y|\ystep\y')
group by di.slug order by di.slug;
```

**2. Whether the contracts produced tables at all:**

```sql
select di.slug,
       count(distinct d.id)                     as contracts,
       count(*) filter (where c.kind = 'table') as table_chunks
from districts di
join documents d on d.district_id = di.id
left join chunks c on c.document_id = d.id
where d.doc_type = 'contract'
group by di.slug order by di.slug;
```

**3. What is loaded:**

```sql
select di.slug,
       (select count(*) from salary_schedule  s where s.district_id = di.id) as salary_rows,
       (select count(*) from stipend_schedule s where s.district_id = di.id) as stipend_rows
from districts di order by di.slug;
```

### Reading the result

- **Candidates exist, `never_tried` is high** — the extraction pass was only
  ever run scoped to Tarrytown. Nothing to build: run **Actions → extract →
  Run workflow**, `district: all`. Start with `limit: 20` and `dry_run: true`
  to see the audit before spending on the full set. This is the most likely
  case.
- **Contracts exist but few or no table chunks** — the salary grids are not
  being detected as tables in those PDFs. That is a `src/herald/pdf_text.py`
  problem and the harder branch. Look at one district's contract PDF and find
  out whether the grid is a real table, a set of positioned text runs, or a
  scan needing OCR.
- **Few or no contracts for those districts** — an acquisition problem, not an
  extraction one. Each district posts CBAs somewhere different;
  `.github/workflows/crawl-contracts.yml` exists as a starting point. Check
  `docs/DATA_SOURCES.md` first.

## Second task, smaller

The one answer that works cites `Tarrytown-TAT-2022-2025.pdf` — a contract
whose term ended June 2025, over a year ago. `ask` dates the figure but never
says the agreement has expired, and goal A is about *current* contracts.

Make an expired contract say so in the citation, and find whether a successor
agreement exists. Do not silently drop expired figures — a stale number
labelled stale is useful; a missing number is not.

## Hard constraints

- **Supabase is not reachable from the container.** Every query above must be
  handed to the user to run in the Supabase SQL editor and paste back. Do not
  burn time debugging the connection — it is a network policy, not a bug.
  Workflows on GitHub runners *can* reach it; your container cannot.
- **The user works from a phone.** Anything that needs running must be a
  `workflow_dispatch` workflow, not a local command. `extract.yml`,
  `tables-db.yml`, `ingest.yml` and `crawl-contracts.yml` already exist.
- **Do not touch branch `claude/bootstrap-fork-script-xed65l`.** It has an open
  PR (#80) and another session is working on it. Branch from `main`.
- The package stays named `herald`, whatever the repo is called.
- Be honest about absent data. "No schedule extracted for X" is a correct
  answer and better than a plausible number.

## Traps

- **`herald-extract run --dry-run` still spends Anthropic API calls.** The flag
  gates only the database write (`extract_schools.py:508`); every candidate
  table is still sent to the model. Use `--limit` on the first pass.
- **Table chunking changes in PR #80.** It rejoins grids that Word split from
  their headers, drops header-only chunks, and labels each table with its
  agenda item. Across 721 agendas that is 2,446 table chunks becoming 2,215.
  Candidate counts will shift once it merges, and contract PDFs are unaffected
  (the change is HTML-only) — but check whether #80 has merged before treating
  a candidate count as stable.
- Existing table chunks in the database were built by the **old** chunker.
  `herald-ingest tables --replace` re-derives them; plain `tables` cannot,
  because `insert_chunks` conflicts on `(document_id, chunk_index)` and does
  nothing.
- `salary_schedule` and `stipend_schedule` report `n_live_tup 0` in
  `pg_stat_user_tables`. That is a stale ANALYZE estimate, not proof they are
  empty. Use `count(*)`.

## Background worth reading

- `docs/STRUCTURED.md` — the extraction design
- `docs/STATUS.md` — where the corpus stands
- `src/herald/extract_schools.py` — module docstring explains the approach
- `data/targets/required_documents.json` — statute-derived list of what each
  district must publish
