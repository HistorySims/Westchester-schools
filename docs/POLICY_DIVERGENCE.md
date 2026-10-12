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

Across **588 policies held by two or more districts**: **72 value divergences
and 41 unit divergences**. The ones that bear on this project directly:

| policy | what it sets | districts |
|---|---|---|
| **2342 Agenda Preparation and Dissemination** | how far ahead the agenda is public | elmsford **2/5 days**, port-chester-rye **3/5**, ossining **7/14**, white-plains **7** |
| 1050 Annual District Election and Budget Vote | petition deadline | tarrytowns **90 days**, white-plains / elmsford **60**, elmsford also **30** |
| 1741-R Home Instruction Regulation | required instructional hours | ossining **900 hours**, port-chester-rye **990** |
| 1925 Interpreters for Hearing-Impaired Parents | notice required | port-chester-rye **5 working days**, elmsford / ossining **10** |
| 1120 School District Records | FOIL notice to employees | mount-vernon **10 working days**, ossining / white-plains **5** |
| 0100-R Non-Discrimination Regulation | complaint investigation clock | mount-vernon **15 days**, port-chester-rye **15 school days + 30 calendar days** |
| 7260 Designation of Person in Parental Relation | how long a designation lasts | greenburgh-central **12 months**, mount-vernon **6** |

Policy 2342 is the one worth a newsletter paragraph on its own: it is the
public-notice window for the agendas this whole corpus is built from, and it
runs from two days to a fortnight depending on the district.

**Same value, different unit** — easier to read past, and sometimes the larger
difference:

| policy | one district | the other |
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

`uv run scripts/policy_divergence.py --rule credit`, all eight districts:

| district | policy | parameter | standard |
|---|---|---|---|
| elmsford | 5100 STUDENT ATTENDANCE | **28 ATEDs** | bright line |
| greenburgh-central | 7110 Comprehensive Student Attendance Policy | **18 absences** | bright line |
| mount-vernon | 7110 COMPREHENSIVE STUDENT ATTENDANCE POLICY | **20 absences** | bright line |
| ossining | 5100 STUDENT ATTENDANCE: COMPREHENSIVE ATTENDANCE POLICY | — | **no provision** |
| peekskill | 5100 PEEKSKILL'S COMPREHENSIVE ATTENDANCE POLICY | **20 absences** | bright line |
| port-chester-rye | 5100 ATTENDANCE | **18 ATEDs** | bright line |
| tarrytowns | 5100 Student Attendance | **18 ATEDs** | bright line |
| white-plains | 5100 ATTENDANCE | — | administrator discretion |

Four districts share one licensed sentence and fill in three different numbers:

> At the high school level, any student with more than **nine** unexcused ATEDs
> for one-half year or **18** unexcused ATEDs for a full year will not receive
> credit for that course.

That is Tarrytowns and Port Chester verbatim. Elmsford's copy reads
**fourteen** and **28** — twice the allowance for the same rule. White Plains
holds the same template with the threshold sentence **absent**, replaced by
"the decision to deny course credit to a student will be made by the House
Administrator". Ossining's attendance policy contains the word "credit" **zero
times**; across all 432 of its policies none ties credit to absence, and its
4710 Grading Systems refers to "course credit policies" given to parents, so
the threshold — if there is one — lives in a handbook and not in board policy.

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

## Coverage: all eight districts

The districts split across two publishers, so the corpus is two snapshots:

| snapshot | districts | policies | how |
|---|---|---|---|
| `boarddocs-policies.jsonl.gz` | greenburgh-central, mount-vernon, tarrytowns, white-plains | 1,071 | BoardDocs policy console |
| `policy-manuals.jsonl.gz` | port-chester-rye, peekskill, ossining, elmsford | 1,484 | BoardPolicyOnline portals |

**2,555 policies, 1,013 policy numbers, 588 held by two or more districts.**

The four portal districts are Blazor Server apps with no fetchable API — the
page is a ~5 KB shell and every byte of policy text arrives over a WebSocket —
so `herald-scrape policy-fetch` drives a real browser and uses the portal's own
bulk export. That is `herald.scrape.policy_manual`, which already existed;
completing the acquisition meant running it, not writing it.

Rebuilding the snapshot:

```bash
uv sync --group browser
uv run herald-scrape policy-fetch --out <dir> \
    --chromium /opt/pw-browsers/chromium --proxy "$HTTPS_PROXY"
uv run scripts/drift_pilot/policy_snapshot.py --raw <dir>
```

The snapshot stores **extracted text, not markup**: the 1,500 fetched policies
are 19 MB of Kendo widget wrappers that `extract_html_text` would only throw
away, against 1.5 MB gzipped as text. Each row keeps the portal's section
permalink (`/b/<slug>/s/<id>`), so a finding still cites the live manual.

**In a container, Chromium needs the egress proxy's CA in its own store.** It
reads NSS, not the system bundle, and NSS was empty here, so every navigation
failed with `ERR_CERT_AUTHORITY_INVALID` — including `example.com`, which is
how to tell this apart from a blocked host:

```bash
apt-get install -y libnss3-tools
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n ccr-agent-proxy \
    -i /root/.ccr/agent-proxy-ca.crt
```

GitHub runners have no such proxy, so `.github/workflows/policy-fetch.yml`
needs none of this.
