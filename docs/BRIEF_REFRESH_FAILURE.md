# Brief: the weekly refresh has been failing since 2026-10-05

A task brief for a separate Claude Code session. Delete it once the work lands.

## What this repo is

A semantic-research corpus of Westchester County NY school-district governance
documents — agendas, minutes, policies, contracts — in Supabase Postgres with
pgvector. `CLAUDE.md` is the orientation; `docs/STATUS.md` is the current state.

## Your task

`.github/workflows/refresh.yml` is the weekly corpus refresh: crawl eight
districts in parallel, then ingest everything in one job. It has run green for
weeks. On **2026-10-05 it failed and has not been re-run since.** Find out why,
fix it, and get a green run.

## What is already known — do not re-derive this

All eight crawl jobs **succeeded**. The `ingest` job failed, and `report`
failed only because it is designed to fail the run when any job does.

Inside the `ingest` job, every step passed except one:

```
✓ Sync deps        ✓ Check secrets        ✓ download-artifact
✓ Resources before ingest
✗ Ingest
✓ Resources after ingest    ✓ Publish ingest report
```

### The decisive measurement

| run | date | ingest step | result |
|---|---|---|---|
| 36444510113 | 2026-09-28 | **5 min 47 s** | success |
| 37338166996 | 2026-10-05 | **7 seconds** | failure |

**Both ran the identical commit, `a2d9e0f2`.** Nothing merged to `main` between
2026-09-22 and 2026-10-05, and `refresh.yml` itself has not changed since
2026-09-05. So this is not a code regression — the code that worked and the
code that failed are the same bytes.

Seven seconds is a startup failure, not a work failure. That rules out the
things a long-running ingest dies of: an OOM during embedding, a timeout, a
disk filling as it writes. It died before it did any real work.

### What makes it stranger

The standalone `ingest` workflow ran **2026-10-06T00:55Z — about nine hours
later — and succeeded.** So whatever it was had cleared by then, or that run
did not touch it. Note it ran a different commit (`8defac6e`) and different
inputs, so it is not a clean control.

### Hypotheses, most to least likely

1. **The Supabase project was paused or read-only.** Free-tier projects pause
   on inactivity and are restricted when over quota; a connection to a paused
   project fails fast, and the project then restores on demand — which would
   explain both the 7-second death and the success nine hours later. The
   corpus has not been sized since **2026-09-07, when it was 296 MB against a
   500 MB tier**, and a great deal has landed since.
2. **A credential problem.** The `Check secrets` step only asserts the
   variables are non-empty, so a rotated or invalid `VOYAGE_API_KEY` passes it
   and dies at the first embed call — comfortably within 7 seconds.
3. **No manifests under `data/artifacts`.** `herald-ingest run` would find
   nothing and exit non-zero almost immediately. The `Resources before ingest`
   step prints the per-manifest entry counts, so **the log settles this one
   instantly.**
4. A transient Supabase or Actions incident, in which case a re-run is green
   and the answer is "nothing to fix" — which is a fine outcome, but confirm
   it rather than assume it.

## Step 1: read the log

Everything above was assembled without it. The log says in one line which
hypothesis is right.

**This matters for how the environment is set up.** Actions logs are served
from `*.blob.core.windows.net`, which the default container cannot reach, and
the built-in `gh` only talks to `api.github.com` and refuses the redirect. The
`check-runs/<id>/annotations` endpoint carries only
`Process completed with exit code 1` — no stderr. So either:

* allow that host in the environment's network policy, or
* ask the operator to open the run in a browser and paste the failing step.

Do not spend an hour working around this. Ask.

Run 37338166996, job 111859370876, step `Ingest`.

## Step 2: whatever the log says

If it is the database, the real question is bigger than one failed run: the
tier has been hit once before (791 MB → 296 MB, `docs/DISK_SPACE.md` records
how and what it cost), and a corpus that grows weekly will hit it again. A
second rescue is worth less than a retention policy. `docs/DISK_SPACE.md` has
the measured per-chunk cost to size one with.

Start with the headroom query, which nobody has run in a month:

```sql
select pg_size_pretty(pg_database_size(current_database())) as total,
       (select count(*) from chunks) as chunks;
```

## Step 3: make the failure legible next time

Separate from the fix, and arguably worth more than it. The run told us
*which step* failed and nothing about *why*, and the log is behind a host this
tooling cannot reach. A failure mode that takes a human with a browser to
diagnose will keep costing that every week.

Worth considering: have the ingest step tee its last lines into
`$GITHUB_STEP_SUMMARY` on failure, the way the `migrate` and `agendas`
workflows already publish their reports. The step summary is readable through
the API, so the next failure diagnoses itself.

## Hard constraints

- **Supabase is not reachable from the container.** Hand SQL to the operator
  and ask them to paste results back; do not debug the connection. Workflows
  on GitHub runners reach it fine — that asymmetry is the point.
- **The operator works from a phone.** Anything runnable must be a
  `workflow_dispatch` workflow.
- **Branch from `main`.** Do not touch `claude/salary-schedules-task-31j1f0`
  or `claude/green-ci-2026-10`.
- **CI is green as of 2026-10-06** (PR #99) after being red for a month. Keep
  it that way — `uv run ruff check src tests`, `uv run pyright`,
  `uv run pytest -q` all pass on `main` right now, so anything red is yours.

## Traps

- **`refresh.yml` encodes several hard-won fixes. Read its comments before
  changing it.** The `ingest` job is `if: always()` so one district's 403 does
  not discard the other seven's documents. `--wave-size 128` is deliberately
  smaller than the throughput-optimal 512 so a killed run loses less.
  `.github/actions/seed-manifest` restores a manifest *only* from a run whose
  own ingest succeeded — the manifest is a proxy for "already in the database"
  and that proxy held falsely once, orphaning 261 documents.
- **A green re-run does not prove a fix.** If a re-run passes, you have
  evidence of transience, not of a cause. Say which you have.
- `n_live_tup` in `pg_stat_user_tables` reads 0 for a never-analyzed table;
  that is not the same as empty. Use `count(*)`.
- This may genuinely be a ten-minute job. If the log names an obvious cause,
  fix it, get a green run and stop — steps 2 and 3 are worth doing, not worth
  inventing work for.

## Background

`docs/REFRESH.md` (how the refresh is meant to work), `docs/DISK_SPACE.md`
(the last tier incident, and the measured per-chunk cost), `docs/STATUS.md`
(current state, including a pinned section on where the salary-schedule thread
stopped).
