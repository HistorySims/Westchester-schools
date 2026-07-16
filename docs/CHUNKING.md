# Chunking & embedding framework

Grounded in the first real corpus (Peekskill/Port Chester BoardDocs pull).
Design goals from the project: put every chunk in **chronological order**,
**filter by district**, exploit the **topic structure the documents already
carry**, and feed **embedding → clustering → trajectory modeling**.

## What the documents actually are

Two shapes, and the ingest must handle both:

1. **Structured agenda / minutes PDF** (Peekskill style) — one PDF is a whole
   meeting: a numbered outline, `1. … 14.`, each with lettered sub-items
   `A./B./C.`, occasionally a third level. Born-digital, clean text — **no OCR
   needed** (a scanned-PDF fallback is a later concern).
2. **Atomic backup doc** (Port Chester style) — an individual contract,
   treasurer report, or bid backup attached to one agenda item. Already a
   single topic.

## The chunk unit: the agenda's own outline

The documents are pre-chunked by humans — the agenda hierarchy is a curated
topic tree. We chunk on it (structural chunking), not with a blind window.

A prototype parser over the real March-17 Peekskill agenda extracts the tree
cleanly for the well-formed parts (`P2.B`, `P13.D`, …) with their section
types. The pathological case is real too: `P11 Consent Agenda – Personnel`
fans out into ~40 repeated "A." blocks and "Name:" lines. So the rule set is a
**hybrid**:

- **Split** on the outline; a leaf item is the default chunk.
- **Merge** fragments below a minimum size into their parent, so the personnel
  list collapses to one "Consent Agenda – Personnel" chunk instead of 40.
- **Sub-split** any item over the embedder's token budget with the existing
  sentence-window chunker (`chunker.py`) as the fallback engine — we keep it,
  we don't throw it away.
- **Atomic backups**: whole doc, size-bounded; doc-level metadata. (Future: we
  know which agenda item a backup was attached to during the crawl — link it so
  the backup inherits that item's section context.)

Granularity is adaptive: letter-level for consent agendas (one chunk per
contract/action — high retrieval value), top-level for narrative sections
(Superintendent's Report as one unit).

## The hierarchy as data — "the topic clustering that already exists"

Every chunk carries its position in, and the meaning of, the agenda tree:

| field | example | purpose |
|---|---|---|
| `section_path` | `["P2","B","3"]` | the addressable position — your "item 3c in B in part 2" |
| `section_path_str` | `P2.B.3` | display / prefix filtering |
| `section_type` | `Consent Agenda - Business/Finance` | the **human topic label** (top-level heading) |
| `heading` | `Southern Westchester BOCES Cooperative Bid 2026/2027` | the item's own title |
| `heading_breadcrumb` | `["New Business","Contract …"]` | semantic ancestry for context |
| `order_index` | `137` | stable position within the document |

`section_type` is the key insight: the same categories recur across every
meeting and district ("Policy Readings", "Consent Agenda – Personnel",
"Hearing of Citizens", "Accepting of Minutes"). That's a **free, human-curated
topic taxonomy** — we get it without any model.

## Proposed schema (replaces the Paper/Issue/Page FORK TODO)

```
District(slug, name, state, tier, ...)                     # one row per district
Document(id, district, doc_type, title, meeting_date,      # one row per PDF
         meeting_type, committee, source_url, sha256, local_path)
Section(id, document_id, parent_id, path, path_str,        # the agenda tree
        section_type, heading, level, order_index)
Chunk(id, document_id, section_path_str, section_type,     # retrieval unit
      heading, content, embedding, chunk_index,
      district, meeting_date, doc_type)                    # <- DENORMALIZED
```

**Denormalize `district`, `meeting_date`, `section_type` onto `Chunk`.** Your
two hard requirements — chronological order and district filtering — then
become single-column `ORDER BY meeting_date` / `WHERE district = …`, no joins,
and they compose with a vector search as cheap metadata filters.

## Embedding strategy

- **Embed contextually.** Prepend a compact context line to each chunk before
  embedding:
  `"{district} · {meeting_date} · {section_type} › {heading}\n\n{body}"`.
  A chunk pulled out of its document loses the context that it's, say, a
  finance contract from Peekskill in March 2026; the breadcrumb restores it.
  This is contextual retrieval (Anthropic's technique) in its cheap,
  deterministic form — no LLM call needed. (Optional upgrade: an LLM-written
  one-line context per chunk, at token cost.)
- **Keep `district` / `meeting_date` / `section_type` as structured columns
  too**, not only in the embedded text — so filtering and sorting are exact,
  while the embedding stays semantic.
- Embedder: Voyage `voyage-3.5` (already wired in `settings.py`); vectors +
  metadata in Postgres/pgvector.

## Clustering

- Cluster the chunk embeddings with the engine's existing UMAP + HDBSCAN
  (`cluster.py`).
- Use `section_type` as a **human-labeled topic taxonomy** to (a) evaluate
  discovered clusters against, (b) seed/compare, or (c) cluster *within* a
  section type. We end up with both model-discovered topics and the documents'
  own curated topics — and can measure how well they align.

## Trajectory modeling (the payoff)

- Axes: **district × meeting_date × topic** (topic = `section_type` or a
  discovered cluster).
- Per `(district, topic)`, the chunks form a chronological series → model
  attention/volume over time, or embedding-centroid drift.
- `cluster_drift.py` already tracks cluster drift across time-buckets (built for
  newspaper weeks) — reuse it with meeting-dates as the buckets.
- The chronological-ordering requirement is the time axis; the district filter
  is the panel dimension. With the denormalized schema, a trajectory is a
  `GROUP BY district, topic ORDER BY meeting_date`.

## Prerequisite fixes (before ingest)

- **Meeting date is load-bearing now.** The crawl currently stamps every file
  with its parent meeting's date (and one parsed to 2026-12-31). For chronology
  we need the document's true meeting date — parse it from the agenda header
  ("MARCH 17, 2026") / title for structured docs; use the parent meeting date
  for backups.
- **doc_type classification**: "Business Meeting …" is an agenda/minutes doc but
  currently lands as `other`. Add meeting/agenda/minutes detection.

## Two content types → two pipelines

The corpus splits cleanly, and each half wants different handling:

- **Narrative** (Superintendent's Report, discussion, Hearing of Citizens) →
  **embed** → topic clustering & **topic trajectory** (semantic drift over time).
- **Enumerated / consent** (personnel, contracts, budget transfers) → these are
  labeled record lists, not prose → **extract** into typed tables → **entity
  trajectory** (a person / vendor / dollar figure tracked over time).

Both come from the same source docs; a section is routed by its `section_type`.

### Entity extraction & entity trajectory (the accountability use case)

The consent agendas are the richest oversight data in the corpus. Personnel
items carry labeled fields — a prototype over the real March-17 Peekskill
agenda pulled 11 clean records straight out:

```
personnel_action(district, meeting_date, category, name, position,
                 location, salary, step, effective_date, source_doc)
# e.g. (peekskill, 2026-03-17, Appointment, Melissa Mackhanlall,
#       Interim Director of Instruction, Administration Bldg, $158,250, 11, 2026-07-01)
```

Once it's a table, entity questions are one-liners — non-renewals, who's
collecting stipends, following one employee across years — none of which vector
search over a merged blob would answer. The same pattern applies to
`contract(vendor, amount, purpose, …)` and `budget_action(amount, purpose, …)`.

Crucially: **merging the personnel *chunk* loses nothing**, because the raw text
is retained (chunk + source PDF) and the person-level power lives in the
extraction table, not the embedding. Extraction can run after the chunking
pipeline — the raw is preserved either way. (All public record: board
personnel actions and public-employee salaries are published for accountability.)

## Decisions

1. **Granularity — adaptive.** Letter-level for consent agendas (one chunk per
   contract/action), top-level for narrative sections.
2. **Personnel — chunk per category block** (Appointment / Resignation /
   Retirement…) for embedding, *and* run structured extraction for per-person
   querying. Not one blob, not per-person fragments — and no snooping capability
   lost, because that lives in the extraction table.
3. **Contextual prefix — deterministic** breadcrumb to start; add LLM-written
   context later only if retrieval needs it.
