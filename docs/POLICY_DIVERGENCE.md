# Board policies: the same rule, priced differently

Board policies are licensed, not written. The districts buy model policy from
the same few vendors, so two manuals run word-for-word identical for pages and
then differ in the one figure that decides something — how many absences cost a
student course credit, how long before an election a petition must be filed.

**This is the one case the drift detector cannot see.** `docs/DRIFT_PILOT.md`
builds a method for prose that was *rewritten*, measuring how far a section
moved in embedding space. Here the wording does not move at all: "eighteen (18)
absences" against "twenty (20) absences" inside identical text is cosine ~0.999,
closer than two paraphrases of the *same* rule. Run on a policy manual, that
method would report two districts as agreeing precisely where they disagree
most. A number inside shared boilerplate is an exact-extraction problem, and a
policy a district deleted is a set difference.

Code: `src/herald/policy_compare.py`, `scripts/policy_divergence.py`,
`.github/workflows/policy-divergence.yml`. Corpus:
`data/snapshots/boarddocs-policies.jsonl.gz`.

## A parameter is a quantity with a unit

That one decision does most of the work. Comparing bare numbers buried every
real finding under citation formatting — `8 N.Y.C.R.R. 200.2(c)` against
`(8NYCRR 200.2(c))` is one citation tokenized two ways, `Version 2` against
`Version 2.0` is nothing at all — and requiring a unit discards all of it
without a single special case for citations. It cut the sweep from 21 findings,
most of them noise, to 9, nearly all of them real.

Units include the local term of art: Tarrytowns states its entire attendance
rule in **ATEDs** (Absence, Tardiness or Early Departure), and omitting that
unit read the district as having no credit rule when it has an explicit 18.

## Two tools, because neither covers the other's cases

### The sweep — same sentence, different number

Align clauses between two districts' copies of one policy, keep the pairs whose
wording matches and whose quantities do not. No question asked in advance, no
threshold to calibrate against ground truth, and it finds things nobody was
looking for. Across **299 policies held by two or more districts**:

**Different value**

| policy | district | value |
|---|---|---|
| 1050 Annual District Election and Budget Vote | tarrytowns | petitions **90 days** in advance |
| | white-plains | petitions **60 days** in advance |
| 1120 School District Records | mount-vernon | FOIL notice to employees within **10 working days** |
| | white-plains | **5 working days** |
| 7260 Designation of Person in Parental Relation | greenburgh-central | designation valid up to **12 months** |
| | mount-vernon | **6 months** |

The election clause is identical in both manuals to 1.000 but for the figure,
and it decides who can get onto a school board ballot.

**Same value, different unit** — easier to read past, and sometimes the larger
difference:

| policy | greenburgh / tarrytowns | mount-vernon / white-plains |
|---|---|---|
| 7613 Implementing a student's IEP | 30 **school** days | 30 days |
| 7512 Student Physicals | 90 **calendar** days | 90 days |
| 9645 Disclosure of Wrongful Conduct | 30 **calendar** days | 30 days |

Thirty school days and thirty days are about six weeks apart.

The sweep only works where the vendor's sentence survived in both copies.

### The probe — name the consequence, read the number

Necessary exactly where the sweep fails. Greenburgh writes "any student with
more than 18 absences in a course may not receive credit"; Mount Vernon writes
a bulleted "20 absences for a one (1) credit course". No sentence alignment
pairs those, so the best-known parameter in the corpus is invisible to the
sweep. The probe instead names the *consequence* — loss of credit — and reads
the quantity attached to it, whatever the surrounding wording.

`uv run scripts/policy_divergence.py --rule credit`:

| district | policy | parameter | standard |
|---|---|---|---|
| greenburgh-central | 7110 Comprehensive Student Attendance Policy | **18 absences** | bright line |
| mount-vernon | 7110 COMPREHENSIVE STUDENT ATTENDANCE POLICY | **20 absences** | bright line |
| tarrytowns | 5100 Student Attendance | **18 ATEDs** | bright line |
| white-plains | 5100 ATTENDANCE | — | administrator discretion |

Three things in that table are worth more than the numbers:

1. **The number is not the rule.** Greenburgh's 18 and Tarrytowns' 18 are not
   the same standard. Greenburgh tallies "all absences excused and unexcused"
   with listed exceptions; Tarrytowns counts **only unexcused** ATEDs. A
   student with nineteen excused absences loses credit in Greenburgh and does
   not in Tarrytowns — a wider gap than 18 against 20.
2. **"No number" is not "no policy."** White Plains sets no threshold at all:
   "the decision to deny course credit to a student will be made by the House
   Administrator", appealable to the Principal. That is a discretionary
   standard, and reporting it as a missing policy would be wrong in both
   directions.
3. **Districts do not agree on the policy number.** Attendance is 7110 in
   Greenburgh and Mount Vernon, 5100 in Tarrytowns and White Plains, so
   alignment by number alone silently splits the peer set in half.

## What this cost to get right

- **The largest number near the phrase is not the parameter.** Mount Vernon's
  policy puts "a student with 20 consecutive absences … can be discharged" two
  sentences from its "no credit" language. The first version reported the
  discharge rule's figure and quoted the discharge sentence as its evidence —
  and that figure *equals* the real credit threshold, so the output looked
  correct while its provenance was wrong. Numbers are now attributed to
  whichever consequence is nearest. A fixed exclusion window cannot do this:
  the discharge verb sits ~75 characters past its number, and a window wide
  enough to catch it also reaches the "summer school" clause that follows the
  genuine credit table, discarding the real threshold instead.
- **Escalation ladders.** Greenburgh contacts a parent at 4 absences, sends a
  letter at 9, opens a contract at 13, denies credit at 18 and bars summer
  school at 27 — all within a few hundred characters. The denial is the rung
  that matters and it is not the first one in range.
- **Spelled-out numbers break alignment.** "12 months" and "six (6) months" are
  the same clause, but blanking only the digits leaves the word behind and the
  two never pair. Both forms have to go.
- **A unit filter also does the appeal-clock work.** White Plains' "appeal
  within five (5) school days" sits inside its credit paragraph and is not a
  threshold; requiring the unit to be an absence-like one drops it.

## Coverage

The policy snapshot holds **4 of 8 districts** — greenburgh-central (302),
mount-vernon (338), tarrytowns (241), white-plains (190), 1,071 policies in all.
Ossining, Peekskill, Port Chester and Elmsford publish through Blazor policy
portals whose text only exists after client render, which is what the
`browser` dependency group and `policy_manual.py` are for (see
`docs/SCRAPING.md`). Completing the peer set is an acquisition job, not a
change to this tool: both functions take district-labelled text and nothing
else.

Until then, every finding here is a statement about four districts. The
attendance comparison in particular is missing Port Chester, which is the
district most of this project is pointed at.
