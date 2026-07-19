# Ask: panel retrieval + cited synthesis

The query layer over the schools corpus (`herald-ask`, workflow `ask`).
This document records the *foundational* design decisions, because the
first surface built shapes everything after it.

## Why not plain RAG

The questions this corpus exists for are **comparative**. Three archetypes,
from the project's own examples:

| Archetype | Example | What breaks in global top-k RAG |
|---|---|---|
| **Norm** | "What's the normal cell-phone policy?" | "Normal" requires evidence from *every* district; top-k lets 2 verbose districts fill all 12 slots |
| **Coverage** | "Which districts are doing Middle States accreditation?" | The answer is a roster, and **absence is part of it** — top-k structurally cannot say "nothing found in Elmsford" |
| **Outlier** | "Which schools pay coaches an abnormal amount?" | "Abnormal" is a cross-district comparison; also ultimately wants structured extraction, not prose retrieval |

## The foundational move: the district is the unit of analysis

**Panel retrieval** (`schools_retrieval.py`): both search legs rank
*per district* — `row_number() over (partition by district_id order by …)`
in SQL — so every district contributes its best candidates, and no
district can crowd out another. Then:

1. **Semantic leg** — pgvector cosine over the contextual-prefix
   embeddings, top-`pool` per district.
2. **Keyword leg** — Postgres FTS (`websearch_to_tsquery`), top-`pool`
   per district. Catches proper nouns ("Middle States") that embeddings
   blur.
3. **RRF fusion per district** (k=60) — a chunk found by both legs
   outranks either alone; fusion never crosses district lines.
4. **One pooled Voyage rerank-2.5 call** over all districts' candidates
   (cheap: one HTTP call), then top-`per_district` (default 4) each.
5. **Per-document cap** (`max_per_doc`, default 2) — no more than 2 chunks
   from any one document per district, so a single long policy/contract
   can't fill the slate with near-duplicates; backfills from the same
   document only if the district has no other relevant source.
6. **Empty districts are returned explicitly** (`Panel.empty_districts`),
   not silently dropped.

At ~25k chunks the window-function scan is exact (no ANN approximation)
and fast. Revisit only past a few hundred thousand chunks.

## Synthesis contract (`ask_schools.py`)

- Evidence is presented **grouped by district**, numbered `[N]`, each
  entry carrying district · date · doc_type · title · §section_path —
  the chunk's full provenance, so every claim is checkable.
- The prompt names the **districts that produced nothing**, and the
  system prompt requires them reported as "no evidence in the retrieved
  documents" — never "district X doesn't do this". Coverage is uneven
  (Greenburgh thin, scanned docs unreadable), so absence is weak evidence.
- Quantitative guardrail: quote figures exactly, attribute district+date,
  refuse "abnormal/highest" verdicts when comparison coverage is thin.
  (The real answer to stipend-outlier questions is the future structured
  extraction pipeline — this layer does honest best-effort from text and
  says so.)
- Recency: prefer newest evidence; present old-vs-new conflicts as change
  over time. `meeting_date` ordering is why ingest fought for real dates.
- Citation validator (inherited from the newspaper engine): hallucinated
  `[N]` → one retry → hard error, never a silently wrong answer.

## Surfaces

- **CLI**: `herald-ask "question" [--districts a,b] [--doc-type policy]
  [--since 2024-01-01] [--per-district N] [--max-per-doc N]
  [--model claude-haiku-4-5] [--evidence-only] [--report out.md]`.
  Each answer's footer reports token usage + an approximate USD cost
  (`_PRICING` table in `ask_schools.py`; Sonnet 5 is on intro pricing
  through 2026-08-31). `--model claude-haiku-4-5` is the cheap lever
  (~3–4× cheaper, no adaptive thinking) at a quality cost on the hard
  comparative questions.
- **Workflow** `ask` (phone path): Actions → ask → type the question →
  cited answer in the run summary. `evidence_only` runs retrieval without
  the synthesis model — no Anthropic key needed — for smoke-testing and
  for eyeballing what retrieval actually returns.

Secrets: `SUPABASE_DB_URL`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`
(synthesis only).

## What this foundation buys later

- The **Panel** object (per-district evidence + explicit absence) is the
  input shape for the future brief/dossier surfaces too — cluster
  matching can replace/augment the retrieval legs without changing the
  synthesis contract.
- `per_district`/`pool`/`--doc-type`/date-window are the tuning surface;
  the SQL legs are cursor-level functions, testable with fakes.
- Chunk ids ride along in `EvidenceChunk`, so a future UI can deep-link
  from a citation to the chunk row and its source document.
