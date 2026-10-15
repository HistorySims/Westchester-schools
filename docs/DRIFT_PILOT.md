# Cross-district drift on safety plans — pilot result

**Question.** Every district must file a district-wide safety plan. If their
centroids drift together, that is Albany; if one drifts alone, that is a
decision. Does that work as a hot-spot detector over a corpus that is mostly
routine compliance?

**Answer.** The idea works. The obvious implementation does not, and the
difference matters enough to be the main finding here.

**Validation test, fixed in advance.** Chapter 155.17 of the Commissioner's
Regulations was amended for 2024-25. If the method cannot recover a mandate it
has been told about, it will not find a quiet local choice. Peekskill named its
own file `... 2024-25 Draft w_155.7 updates.pdf`, so at least one district
knew it was responding to a regulation change. (The plans' own text cites
**155.17**; "155.7" is that district's shorthand.)

## What the corpus is

30 plans, 7 of 8 districts, 2019-20 through 2026-27, pulled from BoardDocs
(32 downloaded, 2 unreadable — see below),
attachments. One document per district-year — the latest attachment of that
year, which is the adopted version rather than a circulated draft.

| district | years held |
|---|---|
| peekskill | 2020-21 … 2026-27 (7) |
| elmsford | 2019-20, 2020-21, 2021-22, 2022-23, 2025-26, 2026-27 |
| greenburgh-central | 2020-21, 2021-22, 2022-23, 2024-25, 2025-26 |
| white-plains | 2020-21, 2022-23, 2023-24, 2024-25 |
| tarrytowns | 2019-20, 2020-21, 2021-22 |
| port-chester-rye | 2022-23, 2023-24, 2024-25 |
| mount-vernon | 2023-24, 2024-25 |

### What is missing, and why

- **Ossining files no plan in BoardDocs.** 444 meetings scanned, zero hits —
  not even a Policy 8130 reading, which every other district produced. Probed
  separately with errors surfaced: 10 of 10 agendas fetched, 0 failures. The
  district simply attaches very little (0–3 files per meeting where peers
  attach dozens). Its plan is public somewhere by law, but not here; that is a
  `site.py` target, not a BoardDocs one.
- **Mount Vernon 2021-22 and 2022-23** are legacy Word `.doc` (OLE2, real
  `WordDocument` stream) that LibreOffice refuses to open and python-docx
  cannot read. Both are pre-mandate years and Mount Vernon keeps 2023-24 and
  2024-25, so the transition under test survives.
- **Four plans exist only as page scans** and were recovered with Tesseract:
  `elmsford 2020-21`, `greenburgh-central 2024-25` and `2025-26`, and
  `peekskill 2026-27`. Provenance is recorded in `ocr.json` and carried through
  every table, because **OCR noise inflates distance exactly like real change
  does** — an untagged OCR'd document is the most likely source of a false
  "moved alone". Deriving that flag from file size instead got it wrong:
  Peekskill's 2026-27 is a 5.8 MB PDF whose text layer is fine.
- `peekskill 2026-27` initially extracted as 107,000 characters of mojibake
  (`!"#$%&$' ! !" ! # $`) from a broken font encoding, and its mandate
  vocabulary counts all came back **zero**. Read literally that was a district
  deleting the state's new language. OCR'ing the same pages gives trauma 6,
  simulat 7, props/actors 13 — consistent with its own 2025-26. A document that
  extracts to garbage reads as a policy reversal, which is worth a guard in the
  ingest path and not just a note here.

## The implementation that failed

One mean-pooled centroid per (district, year), cosine distance between them,
per-district movement against the group's movement. Consensus movement by year
pair:

| transition | consensus moved |
|---|---|
| 2020-21 → 2021-22 | 0.0218 |
| 2024-25 → 2025-26 | 0.0040 |
| 2022-23 → 2023-24 | 0.0035 |
| **2023-24 → 2024-25** | **0.0008** |
| 2021-22 → 2022-23 | 0.0006 |

The mandate year is the **second smallest movement in the series**. Every
district sat within 0.01 of consensus in every year but one. The method
reported a quiet year in the year the regulation changed.

It is not an embedding-quality problem. The change is plainly in the text, and
a word count finds it instantly:

| | 2023-24 | 2024-25 |
|---|---|---|
| "trauma" | 2 | **10** |
| "simulat…" | 0 | **4–5** |
| "props" / "actors" | 0 | **8–10** |
| parent notification | 1 | **4** |

Three districts — peekskill, port-chester-rye, white-plains — jump identically.

**The failure is arithmetic.** A mandated document is mandated in content and
order, so roughly 95% of any two filings is the same words. A change confined
to three sections of an eighty-section document moves the mean-pooled vector by
about a fortieth of its own length, which is well inside the distance between
two districts' boilerplate. Mean-pooling measures what the documents have in
common, and what they have in common is the part the state wrote.

## The implementation that works

Compare sections, and keep the comparison local to the change. For each section
of a filing, find its closest counterpart in the same district's previous
filing and keep that distance. Untouched template re-extracts at ~0.01, so a
rewritten section at 0.15 stands out by an order of magnitude with no threshold
chosen in advance.

**Magnitude, not a count.** The first attempt counted sections with *no* near
neighbour in the prior year and found **zero** changes for Peekskill in
2024-25. The mandate did not arrive as new sections; it arrived as sentences
inserted into sections that already existed, so each rewritten section's
nearest neighbour is still its own previous version at ~0.85 similarity — far
below any threshold that template reformatting does not also trip.

Then the peer test, which is the part a single district's history cannot do:
a passage a peer also filed that year is the mandate arriving; a passage nobody
else filed is a local decision.

### Validation: passed

2023-24 → 2024-25, top changed sections:

| district | top distance | peer match | mandate vocabulary |
|---|---|---|---|
| port-chester-rye | 0.261 | 0.94 | props, actors, simulations |
| peekskill | 0.229 | 0.97 | props, actors, simulations, trauma |
| peekskill | 0.184 | 0.90 | props, actors, simulations |
| white-plains | 0.310 | — | present in top 8 |
| greenburgh-central | 0.167 | 0.84 | **none** |
| mount-vernon | 0.092 | 0.85 | **none** |

The geometry found the mandate sections *and* correctly labelled them shared.
The text it surfaced is the regulation itself:

> `155.17(b)(20-22)` Trauma means an emotional response to a deeply distressing
> or disturbing experience such as, but not limited to…

> Schools and districts that opt to participate in full-scale exercises … that
> include **props, actors, simulations, or other tactics intended to mimic a
> school shooting** … may not be c[onducted]…

Worth stating plainly because an earlier pass got it backwards: this language
**bans** simulated drills. "Simulations, props, actors" reads like a district
adding theatrical drills and is the opposite. Any newsletter summarizing plan
changes will hit this trap, and vocabulary moving is not the same as policy
moving in the direction the vocabulary suggests.

**Two districts did not adopt it.** Greenburgh's and Mount Vernon's 2024-25
plans carry none of the new language — Mount Vernon's median section distance
from its own 2023-24 filing is **0.0000**. Greenburgh picks it up partially in
2025-26; Elmsford not until 2026-27. That is a compliance lag the method
detects as cleanly as the adoption.

### Hot spots: new, and no peer filed anything like it

Restricted to text-layer pairs, since OCR'd documents dominate the raw ranking:

| distance | peer | district | transition | what it is |
|---|---|---|---|---|
| 0.319 | 0.79 | elmsford | 2022-23 → 2025-26 | pandemic annex: essential/non-essential worker designation, hygiene protocols, COOP |
| 0.303 | 0.72 | elmsford | 2025-26 → 2026-27 | data minimization, de-identification, encryption of PII |
| 0.281 | 0.72 | white-plains | 2022-23 → 2023-24 | **appended SRO agreement with the City of White Plains police department** |
| 0.281 | 0.72 | elmsford | 2025-26 → 2026-27 | **student reunification design** — parent holding area, release/reconciliation point, runner's voucher, unauthorized-pickup escort |
| 0.244 | 0.67 | peekskill | 2021-22 → 2022-23 | green cleaning product certification |
| 0.241 | 0.76 | tarrytowns | 2019-20 → 2020-21 | **MOU with Summit Security Services, Inc. for security guards**, plus Sleepy Hollow SRO |

In the OCR'd pairs the same pattern dominates: Elmsford's 2020-21 plan appends
a full SRO agreement with the Village (indemnification, salary, and a clause
designating the officer the district's FERPA "law enforcement unit"), and
Peekskill's 2026-27 appends an agreement with the Peekskill police department.

**The hot spots are overwhelmingly appended inter-agency agreements.** Where a
district broke from the template, it was usually because it bolted a contract
onto the back of its plan — police, private security — or wrote its own
operational annex. These are exactly the roots worth tracing into the agenda
corpus: Tarrytowns contracting Summit Security should have procurement beats,
and those beats are what the braid layer is for.

## What this changes

1. **Centroid drift over whole documents does not work on mandated documents**
   and should not be built on. `drift_schools.py` carries the working operator;
   `consensus_movement` is kept for the group-level question, where a mean is
   meaningful, and not for per-district comparison.
2. **The detector's output is a ranked list of passages, not a score.** Every
   hit above is a quotation with a district, a year and a document behind it,
   which is what a newsletter paragraph needs anyway.
3. **The compliance lag is as findable as the change.** "Two of five districts
   had not adopted the state's new drill language a year after it took effect"
   is a stronger story than any single district's adoption, and it falls out of
   the same run.
4. **Provenance has to travel with the text.** OCR'd and mojibake documents both
   produce confident wrong answers in opposite directions.

## Reproducing

`scripts/drift_pilot/` holds the pilot as run: `plans.py` (crawl for plan
attachments), `reindex.py` (attach real meeting dates), `fetch2.py` (download
one per district-year), `extract_check.py` (readability gate), `analyze.py` (the
centroid attempt, kept because its failure is the finding), `novelty.py` (the
count attempt), `edits.py` (the working magnitude version). They write to a
scratch directory and need `VOYAGE_API_KEY`; Tesseract is required for the four
scanned plans.

---

# Second document class: codes of conduct

Run to test whether the method is a method or a lucky corpus. Every district
must adopt a code of conduct annually under Education Law §2801, and there is a
clean validation target: New York's Distraction-Free Schools Law (§2803)
restricts internet-enabled devices bell-to-bell from the 2025-26 school year.

**Result: detection generalizes, classification does not, and the reason is
worth more than the test.**

## Coverage is much thinner, and that is the first finding

25 filings, 7 districts, 2015-16 … 2026-27 — but spread so thin that most
school years hold one or two districts, against five or six for safety plans:

| school year | districts |
|---|---|
| 2021-22 | 4 — ossining, peekskill, tarrytowns, white-plains |
| 2023-24 | 4 — greenburgh-central, mount-vernon, tarrytowns, white-plains |
| 2019-20, 2026-27 | 2–3 |
| 2015-16 … 2018-19, 2022-23, 2024-25, 2025-26 | 1–2 |

Only three districts have a usable consecutive series: white-plains (6 years),
tarrytowns (5), greenburgh-central (4). Peekskill and Mount Vernon have one
filing each, so neither can be compared with itself at all.

This is not the filters being too strict. An **unfiltered** search for "code of
conduct" across all 721 committed agendas finds **zero** attachments for
port-chester-rye, three for peekskill (all November–December 2021) and three
for mount-vernon (all August–September 2023). Districts adopt the code by
resolution and publish the document on their own website; the safety plan gets
attached to the agenda and the code frequently does not. **Which document
classes are reachable through BoardDocs has to be measured per class, not
assumed from one.**

## Centroid drift fails again, independently

| transition | districts in both | consensus moved |
|---|---|---|
| 2023-24 → 2024-25 | 2 | 0.0002 |
| 2024-25 → 2025-26 | 2 | 0.0003 |
| **2025-26 → 2026-27** | 2 | **0.0001** |

Every adjacent pair lands between 0.0000 and 0.0003, including the pair that
contains the phone law. A second mandated class, a second near-zero reading.

## Section-level detection generalizes

Greenburgh wrote the mandate into its 2026-27 code as prose, and the detector
puts that section **4th of 180** at 0.172 against a median of 0.0020 — about
86× the noise floor, comfortably above p90 of 0.0902:

> Law § 2803 In compliance with New York State Education Phone Free School Law
> where no unsanctioned use of smartphones and other internet-enabled personal
> devices on school grounds in K-12 schools for the entire school day (from
> "bell to bell"), including classroom time and other settings like lunch and
> study hall periods…

That is the whole detection claim, and it holds on a class the thresholds were
not tuned on.

## The peer test does not transfer — districts put one mandate in different documents

Greenburgh's phone section scored a best peer match of **0.79**, one hundredth
under the 0.80 gate, and was labelled a *local* decision when it is the
clearest mandate in the corpus. Ossining's phone language *was* labelled
mandate at 0.85 — but for the wrong reason, and that is the instructive part.

Ossining's vocabulary sits at character 820 of a 1,374-character section whose
first 820 characters are about firearms, firecrackers and explosives, because
it is a footnote hung on a row of a disciplinary matrix. The section's vector
is dominated by weapons language, and its 0.85 agreement with peers is generic
disciplinary-code similarity, not agreement about phones. The footnote names
where the rule actually lives:

> *In accordance with the Distraction-Free Schools Law implemented by New York
> State, the Ossining Union Free School District developed an Internet-enabled
> Device Policy: (New Policy 5695 — Student Use of Internet Enabled Devices
> During the School Day…

**So the same mandate was filed in two different document classes.** Greenburgh
put it in the code of conduct; Ossining wrote a new standalone policy and
cross-referenced it. Nudging the threshold to 0.78 would "fix" this one case
and would be overfitting: the honest reading is that a within-class peer test
asks whether two districts changed *the same document* in the same way, and for
a mandate that is simply not guaranteed. Mandate detection has to search across
classes. Within a class, the **distance ranking transfers and the binary label
does not.**

A second reason the label is fragile here: codes of conduct mix prose with
behavior–consequence matrices, and a matrix row is not a section. Section
comparison assumes the two districts write at comparable granularity, which
holds for safety plans and does not hold here.

## The most useful output on this class: who actually changed anything

Re-filing is the dominant behaviour, and it is detected trivially and
reliably — no thresholds involved.

- **white-plains** 2026-27 is **99.9967% character-identical** to its 2025-26
  code (the PDFs differ, 370 KB against 484 KB, so it was regenerated rather
  than re-uploaded) and contains **zero** occurrences of "internet-enabled",
  "bell-to-bell" or "§2803". The report says so in one line: *"Nothing above
  the floor — this filing restates the previous one."*
- **greenburgh-central** 2023-24 and 2024-25 differ by **4 characters** out of
  223,000.
- **elmsford** 2018-19 and 2019-20 are the same 18.8 MB file re-attached a year
  apart — the same pattern as its safety plan, which went four years unchanged.

"Which districts rewrote their code this year, and which re-filed last year's"
is a question this answers cheaply and exactly, and it is closer to a
newsletter paragraph than any similarity score.

## What the two classes together say

1. **Centroid-per-document is dead** on mandated filings — two independent
   classes, both near zero across every transition.
2. **Section distance ranking is the transferable part.** It found the 155.17
   drill amendments and the §2803 phone law on classes with different
   structure and different thresholds.
3. **The mandate/local label is class-specific** and depends on peers writing
   the same rule into the same document at a similar granularity. Report the
   peer score, do not report the binary.
4. **Coverage must be measured per class.** Port Chester files its safety plan
   every year and its code of conduct never.
5. **Re-file detection is the cheapest real signal in the whole pipeline** and
   needs no embeddings at all.
