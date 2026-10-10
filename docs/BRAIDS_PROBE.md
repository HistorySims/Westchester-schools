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

## 7. Threads ARE recoverable semantically — but embed the subject, not the chunk

The question this probe was really asked: can we track "the district is trying
to improve safety — a study, some RFPs, a contract, then cameras and a security
booth", where nothing is shared but the meaning?

**Yes.** Tested on Port Chester with real Voyage embeddings over 139 agendas.
But only one of two ways of doing it works.

| candidate text | similarity spread | top-20 result |
|---|---|---|
| the whole chunk | 0.38 – 0.52 | noise scores as high as signal: "Approval of Meal Prices" at 0.496 beat "Appointment of the District Safety Team" at 0.407 |
| **the item's subject line** | **0.116 – 0.506**, median 0.214 | nearly all on-topic |

Embedding subjects recovered a five-year lifecycle with no identifier and no
shared string:

```
2022-05-26  Public Comment – District-wide School Safety Plan
2022-08-03  Approval of the District-wide Safety Plan
2023-05-25  Commence a 30 Day Public Comment Period – District-wide Safety Plan
2023-07-27  Approval of the District-wide Safety Plan
2024-05-23  Commence a 30 Day Public Comment Period
2024-08-15  Approval of the District-wide Safety Plan
2025-05-29  Commence a 30 Day Public Comment Period
2025-08-14  Approval of the District-wide Safety Plan
2026-06-18  Commence a 30 Day Public Comment Period
2026-08-27  Approval of the District-wide Safety Plan
```

and, in the same result set, the physical security work that belongs with it:

```
2022-03-21  SEQRA Type II for the JFK walkway and proposed lighting upgrades
2024-01-18  SEQRA Type II Classification for Proposed Capital Improvements —
            Security Vestibules and Lighting
2025-10-16  Superintendent Report — ... Security Presentation
```

Different vocabulary at every step, correctly grouped.

**Why the whole chunk fails.** Every BoardDocs item repeats the same
scaffolding — meeting name, category, type, "BE IT RESOLVED that the Board of
Education of the Port Chester-Rye Union Free School District". Literal
scaffolding measures 22% of characters, and the A/B shows the effect on
similarity is larger than that: two items about unrelated things land ~0.45
apart because most of their text is identical. There is no threshold that
separates signal from noise, which would make BRAIDS stage 2's "top-k above
threshold" shortlist meaningless.

**Recommendation for stage 2: track by subject, cite by chunk.** Assignment
should match on an embedding of the item's distinctive line; the chunk stays
the evidence that gets quoted. The subject is already recoverable with a
regex over the chunk text (`Subject <n.n> <title> Meeting <date>`), and
`chunking.py` could carry it as a field rather than re-deriving it.

This also bears on `ask` and the topic map, which embed the full chunk today.
Worth an A/B there before assuming it is only a braids concern.

## 8. Worked example: Port Chester's safety plan, 2022-2026

Run because the question "can we track a safety initiative" deserved a real
answer rather than a mechanism demo. Three plans fetched from BoardDocs.

| plan | pages | characters |
|---|---:|---:|
| 2022-23 | 45 | 74,248 |
| 2023-24 | 54 | 91,057 |
| 2024-25 | 69 | 108,420 |

**It only ever grows.** Across both transitions, terms added: 18 then 22.
Terms dropped: **zero, both times.** The document accretes and never prunes —
a 53% growth in two years.

### The misreading, recorded because it is the newsletter's main hazard

The 2024-25 plan introduces *shooting, simulations, props, actors, mimic,
tactics, trauma-informed*. Read as a word list that says Port Chester added
full-scale active-shooter simulation drills. The actual sentence:

> "...shall be conducted in a trauma-informed, developmentally, and
> age-appropriate manner and **shall not include props, actors, simulations,
> or other tactics intended to mimic a school shooting**..."

It *bans* them. The vocabulary diff was exactly backwards on the substance.
Same with *panic*, which reads like a purchase and is §2801-a(2)(f) requiring
districts to **consider** silent panic alarms.

What is actually happening: **the plan grows by absorbing new Albany mandates
verbatim** — 2023 added remote-instruction definitions and the panic-alarm
consideration, 2024 added the trauma-informed drill requirements. Almost none
of the growth is local decision-making.

**The lesson for the newsletter is structural, not incidental.** Change
detection by vocabulary finds *that* something changed and reliably
misattributes *who decided it*. A brief claiming "Port Chester adds
active-shooter simulations" would be the precise opposite of the truth, sourced
from a real diff of real documents. Any year-over-year feature needs the
sentence, not the term — and needs to distinguish a mandate absorbed from a
choice made.

### A finding in its own right

Port Chester attached the plan to its agenda in 2022, 2023 and 2024, and
**stopped**. The 2025-08-14 and 2026-08-27 approvals carry no safety-plan
attachment — the 2026 agenda has 32 attachments and none is the plan. The board
still adopts it annually, as Education Law 2801 requires; it is no longer
published alongside the vote. (Not posted *to BoardDocs* — it may live on the
district site; worth checking before the claim is made in print.)

### The security trail exists, in attachments

The storyline that prompted this is real and fetchable, just not in agenda
prose:

```
2022-06-23  Security Services Backup.pdf
2023-07-06  Security Guard Services Backup.pdf
2023-10-19  John Pontillo Cameras Donation High School Backup.pdf
2025-03-20  Security Vestibules JFK Bid Backup.pdf
2025-03-20  Security Vestibules MS & King Street Bid Backup.pdf
```

Altaris is Mount Vernon's, not Port Chester's: `Altaris Consulting Group
2022-23 Proposal.pdf`, 2022-05-17.

**None of these attachments is in the corpus.** The agenda snapshot captured
agenda HTML only. So the documents that carry the substance of this storyline
are one fetch away and currently absent — which is the strongest argument yet
for an attachment backfill alongside the minutes one.

## 9. Three-way A/B: what text should assignment embed?

The obvious fix for §7 was "strip the boilerplate". Measured, it is not.

147 Port Chester agenda items labelled into 8 topics by regex over their
subject lines, then embedded three ways and scored on the operation
assignment actually performs — nearest neighbour, document to document.

```
baseline (always guess the biggest class)      41.8%

A  raw chunk            nearest-neighbour 74.7%   precision@5 66.6%
B  scaffold-stripped    nearest-neighbour 79.5%   precision@5 66.8%
C  subject only         nearest-neighbour 95.9%   precision@5 95.2%
```

**Stripping scaffolding is not worth doing.** Arm B gains 5 points on nearest
neighbour and 0.2 on precision@5 — nothing, against the cost of re-embedding
~66,000 chunks.

**Subject-only takes precision@5 from 67% to 95%**, which is the difference
between a stage-2 shortlist that is a third wrong and one that is almost pure.

### Two caveats on this number, both mine

**A leak.** Topic labels came from a regex over subject lines, and arm C
embeds those same subject lines — so C sees the labelling signal undiluted
while A and B see it buried in body text. That is partly the finding (dilution
is the mechanism) and partly a rigged comparison. Trust the direction; treat
the 29-point gap as flattered. A cleaner run would label from something other
than the text being embedded.

**I measured this three times and got three answers.** An eyeball of a seed
query's top-20 said "night and day". A within-topic vs between-topic mean
similarity test said +0.014, i.e. nothing. The kNN test says it is decisive.
The middle one was the wrong statistic: absolute similarities here all sit
between 0.68 and 0.85, so the *mean* gap is uninformative while the *ranking*
is not — and ranking is what assignment consumes. Worth remembering before
trusting any single embedding metric in this corpus.

### What follows

- Do **not** re-embed `chunks.embedding` to strip scaffolding. No evidence it
  helps, and it is the most expensive change available.
- Put the subject embedding where assignment happens — on the beat or thread
  row, not on every chunk. Beats number in the thousands; chunks in the tens of
  thousands, and the storage is `halfvec` either way.
- `ask` is untouched by this. It does query→document retrieval with a short
  query, which is a different operation from document→document clustering and
  needs its own A/B before anyone changes it.

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
