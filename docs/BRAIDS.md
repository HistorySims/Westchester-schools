# Blobs to Braids — narrative thread tracking

*Status: design spec, not yet built. This is ROADMAP.md §6 ("storylines"),
arriving with a real architecture. Build after the vision-OCR / salary-extract
thread wraps.*

## The problem

The topic map clusters chunks in semantic space: five years of "Curriculum
Adoptions" land in one blob regardless of *when* they happened or *which*
initiative they belong to. That answers "what does this district talk about"
but not the question the corpus exists to answer: **what happened to this
specific issue over time** — a policy proposed, pushed back on, referred to
committee, revised, and finally adopted on a split vote. Clustering collapses
that lifecycle into a static point cloud. Braids model it as a directed path
through time.

Three topologies a thread tracker must represent:

1. **Divergence (fork):** one initiative splits into separate tracks
   (a facilities bond forks into "turf field" and "HVAC" work streams).
2. **Parallel progression:** several active issues advance concurrently
   across successive meeting packets.
3. **Convergence (merge):** independent threads collide into one board
   action ("budget vote absorbs the transportation-contract fight").

## Scope decisions (locked up front)

- **Board docs only.** Beats are extracted from chunks whose
  `doc_type in ('agenda','minutes')` — the only documents with reliable
  `meeting_date` (authoritative, parsed from content at ingest). Contracts,
  budgets, and handbooks enter threads as *referenced objects*, never as
  beats: they have no meeting date to order by.
- **Threads never span districts.** Cross-district questions ("who else
  fought about turf fields?") are answered at the pattern layer — comparing
  threads — not by merging them. This eliminates the largest false-merge
  class by construction.
- **Prose only, active only.** Candidates are `kind='prose'` and
  `status='active'` chunks — table chunks belong to herald-extract, and
  quarantined boilerplate (roll calls, adjournments) is exactly the text
  that would generate junk beats. The score pass is a prerequisite, not an
  option.
- **Beats are to prose what salary rows are to tables.** The whole
  herald-extract discipline is inherited wholesale: cheap model reads raw
  text and emits raw labels; deterministic normalization in `taxonomy.py`;
  idempotent upserts on unique keys; a per-chunk processed marker for
  resumability; `--dry-run` audit invariants before any write; provenance
  on every row.

## Honesty about absence (the constraint that shapes everything)

State outcomes (`ADOPTED`, `FAILED`, `TABLED`) are recorded in **minutes**;
agendas only say what will be discussed. Our minutes coverage is wildly
uneven — Tarrytowns backs up nearly every consent item as its own attachment
(7,587 chunks), while Port Chester's minutes are largely scanned images and
Greenburgh has 339 chunks total. Two consequences:

- A thread whose last beat is `PROPOSED` must distinguish "died quietly"
  from "we don't have the minutes that would show what happened."
  Every thread carries an **evidence-coverage** figure (see schema) derived
  from what fraction of the district's meetings in the thread's active
  window have ingested minutes.
- Cross-district braid comparison reports districts with thin coverage as
  "insufficient evidence," exactly as the analytical path reports districts
  without an extracted salary grid as "not available."

---

## 1. Beat extraction (`herald-braid beats`)

A beat is one discrete event in an issue's life, extracted from one chunk.

**Candidate SQL** (the herald-extract shape):

```sql
select c.id, c.content, c.section_path, c.heading, c.meeting_date,
       d.title, di.slug
from chunks c
join documents d on d.id = c.document_id
join districts di on di.id = c.district_id
where c.kind = 'prose' and c.status = 'active'
  and c.doc_type in ('agenda','minutes')
  and c.beat_extracted_at is null          -- resumable, idempotent
order by c.meeting_date asc                 -- chronological: threads build forward
```

Chronological order matters: assignment (stage 2) links each beat to
*already-known* threads, so processing must sweep time forward.

**Model:** Haiku (`claude-haiku-4-5`) — this is classification + copying,
not reasoning. One call per chunk; a consent-agenda composite chunk may
yield several beats. Full-corpus cost ≈ 23k prose chunks × ~1k tokens ≈
**$30–40 one-time**; monthly increments are pennies.

**Beat JSON the model emits** (raw labels only, normalization is ours):

```json
{
  "beats": [{
    "subject_raw":  "Policy 5030 - Student Wellness",
    "action_raw":   "second reading; referred back to policy committee",
    "state_raw":    "referred to committee",
    "entities":     ["Policy 5030", "Wellness Committee", "Dr. Ortiz"],
    "identifiers":  ["5030"],
    "is_outcome":   false
  }]
}
```

**Splitting rule (in the prompt, explicitly):** an omnibus item yields one
beat **per distinct initiative** — a resolution adopting three policies is
three beats, never one compound beat (a compound beat corrupts all three
lifecycles). But split by initiative, *not* by list item: a package
ratifying 40 personnel actions is **one** beat whose `entities` carry the
names — per-person tracking belongs to the structured/entity pipeline
(the two-pipeline split; see STATUS design decisions), not to narrative
threads. Milestone 1's probe report checks both failure directions:
compound beats (under-splitting) and per-name beats (over-splitting).

**State vocabulary — locked, in `taxonomy.py`** (same pattern as
`CANONICAL_LANES`): `PROPOSED, DISCUSSED, PUBLIC_COMMENT, REFERRED,
REVISED, TABLED, VOTED_ADOPTED, VOTED_FAILED, WITHDRAWN, IMPLEMENTED,
OTHER`. `normalize_state(raw)` maps the model's free text
deterministically; unrecognized → `OTHER`, raw always kept. A wrong state
silently corrupts a lifecycle, so — like lanes — we prefer `OTHER` to a
guess, with a hand-maintained crosswalk for district-specific phrasings
("carried 4-3" → `VOTED_ADOPTED`).

## 2. Thread registry & assignment — deterministic first, LLM last

For each new beat, in strict order:

1. **Hard anchors (no model call).** Exact identifier match against active
   threads in the same district: policy numbers ("5030"), resolution/motion
   numbers, bond referendum names. Agenda `section_path` gives a second
   anchor: the chunker's outline addressing means "P13.D" in consecutive
   packets is often literally the same standing item.
2. **Candidate shortlist (no model call).** Embedding similarity of the
   beat's chunk against each active thread's centroid, plus entity-overlap
   (Jaccard on normalized entities), within a temporal window. Top-k ≤ 3
   candidates above threshold. The matching centroid is an **EMA weighted
   toward recent beats**, not a flat running mean — a 3-year thread's late
   "change order #4" language doesn't resemble its early "should we do
   this" language, and a flat mean dilutes toward the middle. Caveat: an
   EMA *follows* mistakes — if a thread starts wrongly absorbing adjacent
   beats, recency weighting accelerates the hijack where a full mean would
   resist. Hard anchors (which never drift) stay the spine, and the frozen
   `centroid_first` powers a drift tripwire in the audit (below). Tune the
   EMA alpha at milestone 3.
3. **Haiku adjudication (only for the ambiguous remainder).** Show the
   beat plus each candidate thread's summary line; the model answers
   `attach(thread_id)` / `new_thread` / `also_merge(thread_id)`. This is
   the only place the model touches topology, and it chooses from a closed
   list — the failure mode is a wrong pick from k options, never invented
   structure.

**Spawn conservatively, merge reluctantly.** No candidate above threshold →
new thread (cheap to merge later, expensive to unpick a false merge). A
merge requires either a shared hard anchor or adjudication *plus* an
entity-overlap floor.

**Dormancy is a query property, not a state machine.** A thread untouched
for N months is "dormant" only in the sense that assignment stops
shortlisting it (temporal decay). Reintroduction ("the 2023 cell-tower
lease is back") is caught by stage 1 — hard anchors ignore the decay window
— which is exactly how long-gap reintroductions actually surface (the
identifier recurs).

**Identity is append-only (the refresh constraint).** The monthly refresh
(REFRESH.md) will run beats+assignment incrementally. Re-runs must never
reshuffle existing threads — the same reason the cluster plan chose
freeze-and-assign over re-clustering. New beats attach; old assignments
never silently move; a discovered mistake is corrected by an explicit
`superseded_by` edge, not a rewrite. Every assignment records *how* it was
made (`anchor` / `similarity` / `adjudicated`) so mistakes are auditable.

## 3. Schema (migration `0006_braids.sql`)

```sql
create table narrative_threads (
  id            uuid primary key default gen_random_uuid(),
  district_id   uuid not null references districts(id),
  title         text not null,             -- Haiku label, regenerated on growth
  status        text not null default 'active',   -- active|resolved|dormant is derived; stored for query speed
  first_seen    date not null,
  last_seen     date not null,
  beat_count    int  not null default 0,
  evidence_coverage numeric,               -- share of active-window meetings with ingested minutes
  centroid      vector(1024),              -- EMA of member-beat embeddings (see note)
  centroid_first vector(1024),             -- frozen mean of the first beats (drift audit anchor)
  created_at    timestamptz not null default now()
);

create table thread_beats (
  id            uuid primary key default gen_random_uuid(),
  thread_id     uuid not null references narrative_threads(id) on delete cascade,
  chunk_id      uuid not null references chunks(id) on delete cascade,
  district_id   uuid not null,
  meeting_date  date not null,             -- ordering key
  seq           int  not null,             -- position within thread
  state         text not null,             -- canonical, from taxonomy
  state_raw     text not null,
  subject_raw   text not null,
  action_raw    text not null,
  entities      text[] not null default '{}',
  identifiers   text[] not null default '{}',
  assigned_via  text not null,             -- 'anchor' | 'similarity' | 'adjudicated'
  created_at    timestamptz not null default now(),
  unique (thread_id, chunk_id, subject_raw)   -- idempotent re-runs
);

create table thread_edges (
  id            uuid primary key default gen_random_uuid(),
  from_thread   uuid not null references narrative_threads(id) on delete cascade,
  to_thread     uuid not null references narrative_threads(id) on delete cascade,
  kind          text not null,             -- 'forks_from' | 'merges_into' | 'superseded_by'
  at_date       date,                      -- when the fork/merge happened
  evidence_chunk uuid references chunks(id),  -- provenance for the topology claim
  created_at    timestamptz not null default now(),
  unique (from_thread, to_thread, kind),
  -- "no edge without a citable source" as a constraint, not just prose:
  -- forks and merges MUST carry evidence; superseded_by is an audit
  -- correction and may legitimately lack a single evidencing chunk.
  constraint thread_edges_evidence_chk check
    (kind = 'superseded_by' or evidence_chunk is not null)
);

-- traversals: a thread's beats in order / a district's active threads by recency
create index on thread_beats (thread_id, seq);
create index on thread_beats (district_id, meeting_date);
create index on thread_beats using gin (identifiers);   -- hard-anchor lookups
create index on narrative_threads (district_id, last_seen desc);
-- chunks gains beat_extracted_at timestamptz (marker, same as tables/extract)
```

Forks and merges are **edges between threads**, not intra-thread branch
nodes — a fork spawns a child thread linked `forks_from`, a merge closes
threads with `merges_into` pointing at the surviving one. Every topology
claim carries an `evidence_chunk`: no edge without a citable source.

## 4. Audit invariants (`--dry-run`, before any write)

Advisory flags, herald-extract style:

- **Lifecycle order:** an outcome state (`VOTED_*`) with no prior
  non-outcome beat in the thread (suggests a false attach or missed link).
- **Date monotonicity:** `seq` order must match `meeting_date` order.
- **Merge sanity:** `merges_into` where the surviving thread has no beat
  within a window of `at_date`.
- **Explosion tripwire:** threads-per-meeting above a per-district baseline
  (spawn threshold too loose, or quarantine is leaking boilerplate).
- **Zombie tripwire:** share of single-beat threads (too-strict matching —
  everything spawns, nothing attaches).
- **Drift tripwire:** distance between a thread's live EMA centroid and its
  frozen `centroid_first` beyond threshold — the signature of a hijacked
  thread absorbing an adjacent topic (recency weighting amplifies this, so
  it must be watched, not assumed away).

## 5. Integration

```
ingest (prose chunks) ─► score (quarantine) ─► braid beats ─► braid assign
                                                    │
herald-ask --mode auto ◄── router gains 'trajectory' ┘
      (chronological cited dossier per thread)
viz: braid export JSON ─► Sankey/braid diagram   (cluster-map delivery pattern)
```

- **Pipeline position:** downstream of ingest + score (score is a hard
  prerequisite), sibling of herald-extract. Same workflow shape:
  `braid.yml`, dry-run default, report to run summary.
- **Ask:** the router (analytical.py pattern) gains a third mode —
  *trajectory* — for "what happened with X in district Y": resolve X to a
  thread (anchor/similarity), return beats in order, each citing its chunk
  and document. Failure mode stays "no thread found," never a fabricated
  lifecycle.
- **Viz:** a `braid-export` command emits compact JSON (threads, beats,
  edges) the same way herald-cluster feeds the topic map; the braid/Sankey
  view is the ROADMAP's "trajectory view" deliverable.
- **Monthly refresh:** beats+assignment become a fifth chained job in
  REFRESH.md's pipeline (after extract), processing only chunks with
  `beat_extracted_at is null` — incremental by construction.

## 6. Failure modes → mitigations

| Failure | Mitigation |
|---|---|
| Thread explosion (every beat spawns) | Conservative spawn threshold; explosion tripwire in audit; quarantine prerequisite removes boilerplate fuel |
| False merges (worst failure — corrupts two lifecycles) | Merge needs hard anchor or adjudication + entity floor; per-district only; `superseded_by` correction edges; every edge carries evidence |
| Wrong state labels | Locked vocab, deterministic normalization, `OTHER` over guessing, crosswalk for district phrasings |
| Phantom lifecycles from thin evidence | `evidence_coverage` on every thread; dossiers state coverage; comparisons report "insufficient evidence" |
| Identity churn on refresh | Append-only assignment; explicit correction edges; `assigned_via` provenance |
| Consent-agenda noise | Score/quarantine upstream; composite chunks may yield multiple beats (extraction handles) |
| Cost creep | Haiku for both passes; adjudication only on ambiguous remainder; hard anchors are free |

## 7. Milestones (riskiest assumption first)

1. **Beat quality probe (no schema, no writes).** Dry-run beat extraction
   on one district — Peekskill (small, clean chunker history) — emitting a
   human-readable report of chunk → beats. *Gate: do our chunks decompose
   into usable beats at all?* This is the make-or-break unknown; everything
   else is plumbing we've built before. (~$1 of Haiku.)
2. **Taxonomy + migration 0006 + beat writes.** State vocab in taxonomy.py
   with tests; schema; idempotent beat persistence with markers.
3. **Assignment v1: anchors + similarity only.** No adjudication yet —
   measure how far deterministic linking gets (expect: far, for
   identifier-rich threads like policies). Audit report on one district.
4. **Adjudication + fork/merge edges.** The Haiku closed-choice pass;
   topology edges with evidence.
5. **Trajectory mode in ask + braid export for viz.** The payoff surfaces.
6. **Fold into the monthly refresh** as an incremental chained job.

Each milestone lands independently useful; stop-loss after milestone 1 if
beat quality is poor (then the fix is chunking granularity, not braids).
