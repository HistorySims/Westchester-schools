# Braids milestone 1 — probe findings

*2026-10-10. Run from a dev container with no database and no Anthropic key,
against the committed agenda snapshot plus five minutes PDFs pulled fresh from
BoardDocs. `docs/BRAIDS.md` is the design this tests.*

## Verdict

**The gate passes, but the risk has moved.** Milestone 1 asks "do our chunks
decompose into usable beats at all?" They do — BoardDocs chunks are unusually
well-shaped for it. What the probe found instead is that **thread assignment,
not beat extraction, is the hard part**, and that two input problems had to be
fixed before either could be judged.

Do not read this as a green light for the full build. It is a *stop-loss not
triggered*, plus three corrections to the plan.

## 1. Zero-width spaces were quarantining the best source — fixed

Tarrytowns' minutes are Google Docs exports carrying U+200B between every pair
of words: 8,209 in one 23-page file. It is not whitespace to `str.split()`, so
it survives into the quality scorer, which sees almost no dictionary words.

Five of five minutes PDFs sampled: **all quarantined, all active once
stripped.** One scored `dict_word_ratio=0.06` on text that reads perfectly.

Fixed in `sanitize()` (PR #103). **The fix only stops it recurring — re-running
`score` is what recovers the text.**

This was the single most valuable thing the probe found, and it was invisible
to every query we had run, because quarantined chunks are filtered out of
exactly the views anyone looks at.

## 2. The quality gate is miscalibrated for agendas, beyond the ZWSP bug

Measured on 2026 agendas in the snapshot, before the fix above:

| district | chunks | quarantined | of those, mentioning policy / resolution / bond / contract / budget / $ | personnel share of what survives |
|---|---:|---:|---:|---:|
| greenburgh-central | 895 | 202 (23%) | 154 (76%) | 44% |
| mount-vernon | 658 | 11 (2%) | 11 (100%) | 23% |
| peekskill | 697 | 114 (16%) | 106 (93%) | 4% |
| port-chester-rye | 253 | 94 (37%) | 5 (5%) | 6% |

Actual discarded chunks:

```
[ocr_illegible] 2026-2027 Proposed Revised Budget Presentation — Special Meeting
[ocr_illegible] Budget Appropriation Transfers - Robert Half Services
[ocr_illegible] Policy Readings Subject New Agenda Item
[ocr_illegible] Policy: #7110 Comprehensive Student Attendance Policy
```

Mount Vernon's **budget re-vote** is in there. So is a policy number, which is
the single best hard anchor a thread can have.

It also correctly catches "Pledge of Allegiance" and "Adjourn Meeting", so it
is not simply broken. The heuristic is a dictionary-ratio test built for
newspaper OCR, and short structured agenda lines — dense with proper nouns,
dates, codes and ALL-CAPS category names — score like garbage. Every single
quarantine decision in the sample was labelled `ocr_illegible` on text that was
never OCR'd.

Port Chester is the one district where it behaves as intended (5% of its
quarantine has substantive content), which is why this never showed up: the
district we look at most is the one it works on.

**Recommendation:** before beats, re-tune `classify.py` for structured
documents, or exempt `doc_type in ('agenda','minutes')` from the OCR-legibility
test and quarantine them on a procedural-boilerplate rule instead. This is a
prerequisite in the same sense BRAIDS.md already says score is — it just needs
to be the *right* score.

## 3. Deterministic anchors will not carry assignment

BRAIDS.md milestone 3 expects anchors and similarity to "get far, for
identifier-rich threads like policies." Tested on Greenburgh's 20 agendas from
2026:

- **Policy numbers recurring across ≥2 meetings: zero.**
- Non-personnel subjects recurring across ≥3 meetings: 13, and **every one is
  structural** — "Call Meeting to Order", "New Business", "Committee Reports",
  "Ex Officio Student Report", "Superintendent of Schools Report".
- 117 distinct non-personnel subjects across 20 meetings: roughly six unique
  items per meeting, each appearing once under its own name.

So stage 1 (hard anchors) will fire rarely, and stages 2 and 3 — embedding
similarity and Haiku adjudication — will do nearly all the work. Those are the
expensive, error-prone ones, and the spec's cost model assumes adjudication
handles only "the ambiguous remainder."

This is a lower bound, not a refutation: a thread genuinely can run under
changing titles, which is why the design has similarity at all. But the plan
should not assume anchors carry it.

## 4. Personnel is 44% of Greenburgh's surviving agenda text

The spec already handles this correctly — a 40-person package is one beat whose
`entities` carry the names. Worth saying plainly anyway: on the agenda-heavy
districts, nearly half the material will produce beats nobody wants to read.
Expect to filter by category before threads, not after.

## 5. Minutes availability — the acquisition answer

Sampled the 12 most recent meetings per district, looking for minutes as
attachments or as meeting names:

| district | on BoardDocs | in the database | reachable? |
|---|---|---:|---|
| tarrytowns | **10/12** as attachments | 85 | already working |
| greenburgh-central | **4/12** as attachments | 10 | **yes — large gain** |
| port-chester-rye | **1/12** as attachments | 150 (149 pre-2021) | yes, sparse (~monthly) |
| peekskill | 0 attachments, but `2024 Minutes` / `2025 Minutes` / `2026 Minutes` **collection meetings** | 6 | **yes — needs the collection path** |
| mount-vernon | **0/12, none by name** | 0 | **no — appears genuinely absent** |

Three of the four agenda-rich districts can get minutes. Greenburgh and
Peekskill are the real wins, and Peekskill needs the year-collection handling
the crawler already half-knows about (`_MINUTES_MEETING`).

**Mount Vernon is the hole.** No minutes by any route. Its threads can reach
`PROPOSED` and never an outcome, and no acquisition work changes that.

### Correction to BRAIDS.md and to STATUS

I previously concluded Port Chester does not produce minutes, on the strength
of two SQL patterns finding nothing. **That was wrong, and the queries were at
fault.** The 2026-07-30 agenda carries an attachment titled *"Approval of July
7, 2026 Board Meeting Minutes"* — which matches neither `'approval of the
minutes'` nor `'minutes of the % meeting'`. Port Chester posts minutes as
attachments, roughly monthly. The POL § 106 framing in STATUS.md should be
softened accordingly: thin and under-collected, not absent.

## 6. What beats will actually look like

Not the blocker, but worth recording, since it is what the gate asked about.

**Agendas** arrive pre-structured, which is better than the spec assumed:

```
Subject   9.4 Conference(s)
Meeting   Feb 26, 2026 - Board of Education Meeting
Category  9. Consent Agenda
Type      Action (Consent)
Recommended Action  RESOLVED, that the Board of Education approves ...
```

`Subject` → `subject_raw`, `Type` → a strong prior on `state`, the resolution
text → `action_raw`. Little inference required.

**Minutes** carry the outcomes the agendas cannot:

```
CARRIED 6-0-0
Trustee Paine moved, seconded by Trustee Fletcher, that it be
RESOLVED: That the Board of Education enters into Executive Session ...
```

Mover, seconder and tally are all present and regular enough to parse
deterministically — a vote line may not need a model call at all.

## What this probe did not do

- **No beats were extracted at volume, and Haiku was never run.** There is no
  `ANTHROPIC_API_KEY` in the dev container. Chunk *shape* was assessed by
  reading; chunk → beat quality at scale is still unmeasured, and a probe run
  by a large model would be an optimistic bound on Haiku's output anyway.
- **No database access.** Everything here is from the committed snapshot and
  five freshly fetched PDFs, so corpus-wide counts are inferred from samples.
- Tarrytowns agendas were fetched but not analysed in depth.

## Recommended order

1. **Merge PR #103, then re-run `score`.** The ZWSP fix recovers Tarrytowns'
   minutes; nothing else gets better until it does.
2. **Re-tune the quality gate for structured documents** (§2). Quarantining
   budget re-votes and policy numbers poisons braids, `ask` and the topic map
   alike.
3. **Backfill minutes** for Greenburgh and Peekskill via the snapshot path that
   worked for agendas. This decides whether half the corpus can have endings.
4. **Then** milestone 2 onward — and revise milestone 3's expectations: anchors
   will not carry assignment.
