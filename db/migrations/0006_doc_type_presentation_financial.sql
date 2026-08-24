-- ============================================================
-- Two document types the corpus was missing: 'presentation' and 'financial'.
--
-- PRESENTATION
-- A board slide deck is not a budget, and typing it as one is actively
-- harmful. Ossining's "Directors' Budget Presentation" is 56 pages of which
-- 46 are flat images; what text it has is promotional ("Strengthening Our
-- Digital Infrastructure"), and it currently sits in doc_type='budget'
-- alongside the actual budget books. A question about a spending line can
-- retrieve a slide with an arrow icon reading "An increase in teaching
-- salaries" instead of the book line carrying the figure — topically
-- perfect, structurally incapable of answering. That is the exact failure
-- data/eval/schools_cases.json calls `unanswerable-by-document-class`.
--
-- Separating them fixes budget retrieval today, and makes "what is each
-- board highlighting?" a filterable question rather than an impossible one:
-- persuasion and record are different genres and the corpus could not tell
-- them apart.
--
-- FINANCIAL
-- Treasurer's reports, audits, warrant/check registers and claims-auditor
-- reports are financial documents but not spending plans. They were
-- deliberately kept out of 'budget' (so "show me the budget" would not
-- return audit reports) and had nowhere else to go, so 43 Treasurer's
-- Reports sat in 'other' — the bucket nothing filters on.
--
-- Apply:
--   uv run herald-migrate apply          (or the `migrate` workflow)
-- ============================================================

alter table documents
  drop constraint if exists documents_doc_type_chk;

alter table documents
  add constraint documents_doc_type_chk check (doc_type in
    ('minutes','agenda','policy','handbook','contract','budget',
     'transcript','presentation','financial','other'));

-- chunks.doc_type is denormalized from documents and carries no constraint
-- of its own; herald-ingest's reclassify updates both in one transaction.

-- Retrieval filters on (district, doc_type) constantly and both new types
-- are meant to be filtered *in* (financial) and *out* (presentation), so
-- make sure that path is indexed.
create index if not exists documents_district_type_idx
  on documents (district_id, doc_type);
