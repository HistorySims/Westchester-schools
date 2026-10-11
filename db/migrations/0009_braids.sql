-- ============================================================
-- Narrative threads (docs/BRAIDS.md) — beats, threads, and the edges
-- between them.
--
-- A beat is one discrete event in an issue's life; a thread is a chain of
-- beats in time; an edge records a fork or a merge. The point is to answer
-- "what happened to X", which clustering cannot: a topic collapses five years
-- of an issue into a point cloud, while a thread keeps the order and the
-- states that make it a story.
--
-- Numbered 0009, not 0006 as the spec says — 0006 through 0008 were taken
-- while that document sat unbuilt.
--
-- Apply:
--   herald-migrate apply          (Actions -> migrate -> Run workflow)
-- ============================================================

-- narrative_threads ---------------------------------------------------------
create table if not exists narrative_threads (
  id            uuid primary key default gen_random_uuid(),
  district_id   uuid not null references districts(id) on delete cascade,
  title         text not null,
  -- Derived from last_seen and the beats, but stored so "what is live in this
  -- district" is an index lookup rather than a scan.
  status        text not null default 'active',
  first_seen    date not null,
  last_seen     date not null,
  beat_count    int  not null default 0,

  -- What share of the district's meetings inside this thread's active window
  -- have ingested minutes. A thread whose last beat is PROPOSED has to be able
  -- to distinguish "died quietly" from "we do not hold the minutes that would
  -- say". Mount Vernon has no minutes at all and Port Chester's are sparse, so
  -- for them this is near zero and every thread must say so rather than imply
  -- a quiet death.
  evidence_coverage numeric,

  -- halfvec, not vector: chunks.embedding was converted in 0007 and there is
  -- no halfvec <=> vector operator, so a vector centroid here could never be
  -- compared against anything.
  --
  -- These hold embeddings of the beats' SUBJECT lines, not their chunk text.
  -- Measured (docs/BRAIDS_PROBE.md §9): matching on whole chunks gives
  -- nearest-neighbour topic accuracy of 74.7% and precision@5 of 66.6%;
  -- matching on subjects gives 95.9% and 95.2%. Every BoardDocs item repeats
  -- the same scaffolding, and on a whole chunk that template is most of what
  -- the vector sees.
  centroid       halfvec(1024),   -- EMA over member beats, weighted to recent
  centroid_first halfvec(1024),   -- frozen; the drift tripwire compares to this

  created_at    timestamptz not null default now(),
  constraint narrative_threads_status_chk
    check (status in ('active', 'resolved', 'dormant'))
);

create index if not exists narrative_threads_district_recent_idx
  on narrative_threads (district_id, last_seen desc);

-- thread_beats --------------------------------------------------------------
-- One row per agenda item, not per chunk. The chunker emits a chunk per agenda
-- *section*, and a section holds several items — Port Chester's 2026-02-26
-- agenda puts 8.1 through 8.5 in one 3,191-character chunk. One beat per chunk
-- would compound five unrelated initiatives into a single lifecycle, which is
-- the failure that corrupts every thread it touches.
create table if not exists thread_beats (
  id            uuid primary key default gen_random_uuid(),
  thread_id     uuid not null references narrative_threads(id) on delete cascade,
  chunk_id      uuid not null references chunks(id) on delete cascade,
  district_id   uuid not null references districts(id) on delete cascade,

  meeting_date  date not null,     -- ordering key; authoritative, from content
  seq           int  not null,     -- position within the thread

  item_number   text not null default '',   -- "8.1" where the district numbers
  state         text not null,              -- canonical, from taxonomy
  state_raw     text not null,              -- always kept; a miss is recoverable
  subject_raw   text not null,
  action_raw    text not null default '',
  entities      text[] not null default '{}',
  identifiers   text[] not null default '{}',

  -- Attachments are where the substance lives. Port Chester's agendas say only
  -- "Approval of the District-wide Safety Plan"; the plan itself, the guard
  -- services contract and the security vestibule bids are all attached files.
  attachments   text[] not null default '{}',

  -- 'anchor' | 'similarity' | 'adjudicated' — so a wrong assignment can be
  -- traced to the stage that made it.
  assigned_via  text not null,

  created_at    timestamptz not null default now(),

  -- Idempotent re-runs. Keyed on the thread as well as the item because a
  -- correction moves a beat by adding a superseded_by edge rather than by
  -- rewriting history; see BRAIDS.md "identity is append-only".
  unique (thread_id, chunk_id, subject_raw),
  constraint thread_beats_assigned_via_chk
    check (assigned_via in ('anchor', 'similarity', 'adjudicated'))
);

create index if not exists thread_beats_thread_seq_idx on thread_beats (thread_id, seq);
create index if not exists thread_beats_district_date_idx
  on thread_beats (district_id, meeting_date);
-- Hard-anchor lookups: does any live thread already carry policy "5030"?
create index if not exists thread_beats_identifiers_idx on thread_beats using gin (identifiers);

-- thread_edges --------------------------------------------------------------
-- Forks and merges are edges between threads, not branch nodes inside one: a
-- fork spawns a child linked back, a merge closes threads pointing at the
-- survivor.
create table if not exists thread_edges (
  id             uuid primary key default gen_random_uuid(),
  from_thread    uuid not null references narrative_threads(id) on delete cascade,
  to_thread      uuid not null references narrative_threads(id) on delete cascade,
  kind           text not null,
  at_date        date,
  evidence_chunk uuid references chunks(id) on delete set null,
  created_at     timestamptz not null default now(),

  unique (from_thread, to_thread, kind),
  constraint thread_edges_kind_chk
    check (kind in ('forks_from', 'merges_into', 'superseded_by')),
  -- "No edge without a citable source", as a constraint rather than a
  -- convention. superseded_by is exempt: it is an audit correction and may
  -- legitimately have no single document evidencing it.
  constraint thread_edges_evidence_chk
    check (kind = 'superseded_by' or evidence_chunk is not null),
  constraint thread_edges_no_self_loop check (from_thread <> to_thread)
);

create index if not exists thread_edges_from_idx on thread_edges (from_thread);
create index if not exists thread_edges_to_idx   on thread_edges (to_thread);

-- resumability --------------------------------------------------------------
-- Same marker pattern as tables_extracted_at and extracted_at: a chunk already
-- walked is skipped, so the monthly refresh processes only what is new.
alter table chunks add column if not exists beat_extracted_at timestamptz;

-- Deliberately NOT stored: a per-beat embedding. Assignment needs a vector for
-- the beat's subject, but a subject line is short and cheap to re-embed, while
-- a halfvec(1024) on every beat would cost roughly 2 KB a row against a 500 MB
-- tier that is already more than half spent (docs/DISK_SPACE.md). Thread
-- centroids are a few hundred rows and are worth keeping; tens of thousands of
-- beat vectors are not.
